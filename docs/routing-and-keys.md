# Routing and key management

Two modules cooperate here. `core/key_manager.py` owns key storage, encryption, and
"which key should I use". `routing/router.py` owns "what do I do when that key fails".

## Key storage and encryption

`core/key_manager.py` exposes three module-level functions over Fernet (AES-128-CBC with an
HMAC), plus a `KeyManager` singleton named `key_manager`.

| Function | Behaviour |
|---|---|
| `encrypt_key(plain)` | Fernet-encrypts, returns the token as a string |
| `decrypt_key(token)` | Reverses it |
| `mask_key(plain)` | `first4...last4`, or `****` when the key is 10 characters or fewer |

`_get_fernet()` raises `RuntimeError` if `ENCRYPTION_KEY` is empty, and the message includes
the command to generate one. This is lazy — it fires on the first key operation, not at
startup.

Keys are never returned in plaintext by any HTTP route. `list_keys()` decrypts only to
compute the mask, then deletes `api_key_encrypted` from the dict it returns. A row that
fails to decrypt gets `api_key_masked = "****"` rather than raising, so one key encrypted
under a rotated `ENCRYPTION_KEY` does not break the whole listing.

## Key selection

`KeyManager.get_available_key(provider)` returns `(decrypted_key, key_id)` using two queries.

**Query 1 — keys with quota.** Enabled keys for the provider where
`rate_limit_remaining_tokens IS NULL` **or** `> MIN_TOKENS_THRESHOLD` (100), ordered so that
`NULL` sorts first, then by remaining tokens descending:

```sql
ORDER BY CASE WHEN rate_limit_remaining_tokens IS NULL THEN 0 ELSE 1 END,
         rate_limit_remaining_tokens DESC
```

`NULL` means "never used, or reset by the sweeper" and is treated as **unlimited**, so fresh
keys are preferred over partially-consumed ones. The `CASE` expression is necessary because
plain `DESC` would sort `NULL` last in SQLite.

**Query 2 — fallback.** If nothing has quota, pick the enabled key with the soonest
`rate_limit_reset_tokens` and log a warning. Serving a request against a probably-exhausted
key and letting the 429 path handle it is better than refusing outright.

If there are no enabled keys at all, it raises `RuntimeError`.

`get_all_keys(provider)` returns every enabled key for the provider ordered by id,
regardless of exhaustion state. The router uses it to bound its retry loop.

## The failover state machine

`routing/router.py:50-164`, `Router.route_request`.

### Provider resolution

The provider is `request.provider` if present, otherwise the router's own default, which is
`SWITCHBOARD_PROVIDER` (or `"groq"`). An unknown name raises `ValueError` — checked both in
`__init__` and again per request, because a per-request `provider` bypasses the constructor.

The registry is a plain dict at `routing/router.py:34`:

```python
{"groq": GroqProvider, "google": GoogleProvider, "anthropic": AnthropicProvider}
```

### The loop

The router fetches all enabled keys for the provider and iterates. On attempt 0 it calls
`get_available_key()` to get the *best* key; on later attempts it walks the list in id
order. A `tried_key_ids` set skips any key already attempted, so the best-key and
walk-the-list strategies cannot double-try the same key. Every attempt after the first
increments `KEY_SWITCHES` and logs.

A fresh `provider` instance is constructed per attempt, because the API key is a constructor
argument.

### Per-outcome behaviour

| Outcome | Action | Metric status label |
|---|---|---|
| Success | Update rate limits from headers, record metrics and usage, **return** | `success` |
| HTTP 429 | `mark_key_exhausted(key_id)`, continue to the next key | `rate_limited` |
| HTTP 401 | `toggle_key(key_id, enabled=False)` — the key is invalid, disable it permanently — continue | `auth_error` |
| HTTP 5xx | Continue to the next key. The key is fine; the vendor is not | `server_error` |
| Other 4xx | **Raise immediately.** A malformed request will fail identically on every key | `client_error` |
| Any other exception | Log and continue | `error` |

The 401 case is the only one with a lasting side effect on your configuration: the key stays
disabled until you re-enable it via `PATCH /admin/keys/{id}`.

### Terminal failure

When the loop exhausts every key:

```
All API keys exhausted for provider 'groq'. Tried 3 key(s). Last error: ...
```

`gateway/main.py` catches this and returns HTTP 502.

```
   +-- get_all_keys(provider) --> RuntimeError if none --> 502
   |
   v
for each key (best first, then by id, skipping tried)
   |
   +-- success ------------------> update limits, metrics, usage, RETURN
   +-- 429 --> mark exhausted ---> next key
   +-- 401 --> disable key ------> next key
   +-- 5xx ----------------------> next key
   +-- other 4xx ----------------> RAISE (no retry)
   |
   v
all keys tried --> raise "All API keys exhausted" --> 502
```

### On success

Four things happen before the result is returned:

1. `update_rate_limits(key_id, result.rate_limit_headers)`.
2. `PROVIDER_REQUESTS`, `PROVIDER_LATENCY`, and `TOKENS_PROCESSED` (twice — once for
   `direction="input"`, once for `"output"`) are incremented.
3. `record_usage(key_id, total_tokens)` writes the minute bucket. This is wrapped in its own
   try/except: a usage-recording failure logs a warning but does not fail a request that
   already succeeded upstream.
4. `result.key_id` is set so callers can see which key served it.

## Rate-limit accounting

### Header parsing

`parse_rate_limit_headers(headers)` maps four Groq/OpenAI-compatible headers onto database
columns:

| Header | Column | Parsed as |
|---|---|---|
| `x-ratelimit-remaining-tokens` | `rate_limit_remaining_tokens` | `int`, silently skipped if unparseable |
| `x-ratelimit-remaining-requests` | `rate_limit_remaining_requests` | `int`, same |
| `x-ratelimit-reset-tokens` | `rate_limit_reset_tokens` | `str`, kept verbatim |
| `x-ratelimit-reset-requests` | `rate_limit_reset_requests` | `str`, kept verbatim |

Absent headers are simply omitted, and `update_rate_limits` returns early if nothing was
parsed — a provider that sends no rate-limit headers leaves the key's quota columns `NULL`,
which the selector reads as unlimited.

The adapters collect these by prefix: every response header whose lowercased name starts
with `x-ratelimit` is passed through in `ProviderResult.rate_limit_headers`.

### Duration parsing

`parse_duration_to_seconds` handles Groq's duration format — `"1m6s"`, `"6.123s"`,
`"59m59s"`, `"500ms"` — by summing three independent regex matches for minutes, trailing
seconds, and milliseconds. The minutes pattern is `(\d+)m(?!s)` so that `500ms` is not read
as 500 minutes.

**It never returns 0.** Anything that parses to zero or fails to parse returns `60.0`. A
zero would mean "already reset", which would make an exhausted key immediately selectable
again.

### Absolute reset time

`update_rate_limits` converts the relative duration into an absolute UTC ISO timestamp in
`rate_limit_resets_at`, preferring the tokens header over the requests header. This is the
column the background sweeper reads; the raw duration strings are kept only for display.
`last_used_at` is stamped on the same update.

### Marking exhaustion

`mark_key_exhausted(key_id)` sets both remaining counters to 0 and, via
`COALESCE(rate_limit_resets_at, ?)`, sets a 60-second default reset **only if one is not
already known**. A real reset time parsed from headers always wins over the default.

### The sweeper

`reset_expired_keys()` NULLs `rate_limit_remaining_tokens`, `rate_limit_remaining_requests`,
and `rate_limit_resets_at` for every enabled key whose reset time has passed, returning them
to the "unlimited" state at the front of the selection order. It runs every 5 seconds from
`_rate_limit_sweeper` and logs only when it actually reset something.

Note the `is_enabled = 1` filter: a key disabled by the 401 path is never swept back into
rotation.

## Seeding from the environment

`seed_from_env()` runs once at startup. If `GROQ_API_KEY` is set **and** the `api_keys`
table has no `groq` rows, it inserts that key with the label `env-default`. The row count
check makes it idempotent across restarts, and it means editing `GROQ_API_KEY` in `.env`
after the first run has no effect — the database is authoritative from then on. There is no
equivalent seeding for Google or Anthropic keys; add those through the admin API.

## Known gaps

- Provider adapter coverage is thinnest exactly where failover lives. See
  [development.md](development.md).
- `MIN_TOKENS_THRESHOLD` is a token count compared against a request whose size is unknown
  in advance, so a key just above the threshold can still 429. The failover path is what
  makes that acceptable.
