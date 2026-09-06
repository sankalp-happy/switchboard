# API reference

Base URL is the gateway itself (`http://localhost:8000` by default). Every route except
`/health` and the `/docs` shell requires a bearer token — see [auth.md](auth.md).

## Route table

| Method | Path | Guard | Defined at |
|---|---|---|---|
| POST | `/v1/chat/completions` | `require_client` | `gateway/main.py:245` |
| GET | `/health` | none | `gateway/main.py:312` |
| GET | `/metrics` | `require_admin` | `gateway/main.py:237` |
| GET | `/openapi.json` | `require_admin` | `gateway/main.py:202` |
| GET | `/docs` | none (empty shell) | `gateway/main.py:214` |
| POST | `/admin/keys` | `require_admin` | `gateway/admin.py:44` |
| GET | `/admin/keys` | `require_admin` | `gateway/admin.py:55` |
| DELETE | `/admin/keys/{key_id}` | `require_admin` | `gateway/admin.py:62` |
| PATCH | `/admin/keys/{key_id}` | `require_admin` | `gateway/admin.py:71` |
| GET | `/admin/keys/usage` | `require_admin` | `gateway/admin.py:103` |
| GET | `/admin/providers` | `require_admin` | `gateway/admin.py:80` |
| GET | `/admin/stats` | `require_admin` | `gateway/admin.py:132` |

`/redoc` is deliberately absent: ReDoc has no clean way to attach an auth header to its
schema fetch, and the schema is admin-guarded.

---

## POST /v1/chat/completions

OpenAI-compatible chat completion. Accepts either a client or an admin token.

### Request body — `ChatCompletionRequest` (`core/schemas.py:8`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | `str` | required | Passed through to the provider verbatim |
| `messages` | `List[ChatMessage]` | required | Each `{role: str, content: str}` |
| `temperature` | `float?` | `0.7` | |
| `stream` | `bool?` | `false` | `true` is **rejected** with 400, see below |
| `provider` | `str?` | `null` | `groq`, `google`, or `anthropic`. Falls back to `SWITCHBOARD_PROVIDER` |

`provider` is stripped from the payload before it is forwarded upstream
(`model_dump(exclude={"provider"})`), so it never leaks to the vendor.

### Response body — `ChatCompletionResponse` (`core/schemas.py:25`)

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1756000000,
  "model": "llama-3.1-8b-instant",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

### Response headers

| Header | Set on hit | Set on miss | Meaning |
|---|---|---|---|
| `X-Cache` | `HIT` | `MISS` | Whether the semantic cache served it |
| `X-Semantic-Similarity` | always | only if a comparison ran | Cosine similarity of the closest cached prompt, 4 decimal places |
| `X-Provider` | no | yes | Which provider served the completion |
| `X-Latency-Ms` | no | yes | Upstream response time, 1 decimal place |

`X-Provider` and `X-Latency-Ms` are absent on a cache hit because no provider was called.
`X-Semantic-Similarity` is omitted on a miss when no comparison happened at all — an empty
prompt, or the cache disabled because `GOOGLE_API_KEY` is unset.

### Status codes

| Code | When |
|---|---|
| 200 | Success, from cache or provider |
| 400 | `stream: true` |
| 401 | Missing or malformed `Authorization` header |
| 403 | Well-formed token that is not in `CLIENT_TOKENS` union `ADMIN_TOKENS` |
| 422 | Body fails Pydantic validation |
| 502 | Provider call failed, or every key for the provider is exhausted |

### The streaming rejection

```json
{
  "error": {
    "message": "Streaming is not supported in this version of SwitchBoard.",
    "type": "unsupported_parameter",
    "param": "stream"
  }
}
```

Returned with HTTP 400. `stream: false` and omitting `stream` both behave normally. This
replaced a silent downgrade in which the adapters forced `stream` back to `false` and the
caller received one complete object with a 200 — an error is strictly better, because the
caller learns the truth.

---

## GET /health

The only unauthenticated route. Docker healthchecks and orchestration depend on it.

```json
{"status": "ok", "version": "...", "cache": "ok"}
```

| Field | Values |
|---|---|
| `status` | always `"ok"` |
| `version` | read from the `VERSION` file at import; `"unknown"` if unreadable |
| `cache` | `"ok"`, `"unavailable"`, or `"unknown"` before the startup probe has run |

**Always returns HTTP 200**, even when `cache` is `"unavailable"`, so a degraded cache does
not fail a container healthcheck. Check the field, not the status code. `cache` tracks Redis
reachability only — a missing `GOOGLE_API_KEY` disables semantic caching but still reports
`ok` here, because Redis itself is fine.

---

## GET /metrics

Prometheus exposition format, behind `require_admin`. See [observability.md](observability.md)
for the metric list and why this route is guarded.

## GET /openapi.json and GET /docs

`/openapi.json` is admin-guarded — it enumerates every admin route, including the request
body for adding a provider key. `/docs` is an unauthenticated static Swagger shell carrying
no data: a browser navigating to it cannot send an `Authorization` header, so guarding the
page itself would make it unloadable. Its inline JS prompts for the token, stores it in
`localStorage` under `switchboard_admin_token`, and attaches it when fetching the schema.

---

## Admin API

All routes are mounted under `/admin` with `Depends(require_admin)` declared **on the
router** (`gateway/admin.py:23`), not on the `include_router()` call — so the guard travels
with the routes through any remount or refactor.

### POST /admin/keys

Request `AddKeyRequest`:

```json
{"provider": "groq", "api_key": "gsk_...", "label": "personal-key"}
```

`label` defaults to `""`. `provider` is lowercased before storage. Response:

```json
{"id": 3, "message": "Key added successfully"}
```

Raises `RuntimeError` (rendered as 500) if `ENCRYPTION_KEY` is unset.

### GET /admin/keys

Optional `?provider=groq` filter. Returns every column of `api_keys` **except**
`api_key_encrypted`, which is deleted from the dict and replaced by `api_key_masked`
(first 4 and last 4 characters, or `****` for keys of 10 characters or fewer).

```json
{"keys": [{"id": 1, "provider": "groq", "label": "env-default", "is_enabled": 1,
           "api_key_masked": "gsk_...aB2c", "rate_limit_remaining_tokens": null,
           "rate_limit_resets_at": null, "last_used_at": null, "created_at": "..."}]}
```

If a row fails to decrypt, `api_key_masked` is `"****"` rather than an error — one key
encrypted under a rotated `ENCRYPTION_KEY` does not break the whole listing.

### DELETE /admin/keys/{key_id}

`{"message": "Key deleted"}`, or 404 `{"detail": "Key not found"}`. The foreign key on
`key_usage_buckets` cascades, so the key's usage history is deleted with it.

### PATCH /admin/keys/{key_id}

Body `{"is_enabled": true}` or `{"is_enabled": false}`. Returns `{"message": "Key enabled"}`
or `{"message": "Key disabled"}`, or 404. Disabled keys are excluded from every selection
query — this is also the mechanism the router uses to retire a key that returned 401.

### GET /admin/keys/usage

Per-key token and request counts over two windows, built by calling `get_usage_stats(1440)`
and `get_usage_stats(1)` and merging them by key id. Keys with no traffic appear with zeros
(the underlying query is a `LEFT JOIN`).

```json
{"keys": [{"id": 1, "label": "env-default", "provider": "groq",
           "last_24h": {"request_count": 42, "total_tokens": 91234},
           "last_1m":  {"request_count": 2,  "total_tokens": 3120}}]}
```

Bucket resolution is one minute, so `last_1m` is the current minute bucket and can read as
0 immediately after a request in the previous minute. Retention is 25 hours, so `last_24h`
is always fully covered.

### GET /admin/providers

Aggregates the key list by provider.

```json
{"providers": [{"provider": "groq", "total_keys": 3, "enabled_keys": 2, "keys_with_quota": 1}]}
```

`keys_with_quota` counts keys whose `rate_limit_remaining_tokens` is `NULL` (never used,
treated as unlimited) or greater than 100 — the same threshold the router uses. Note it
counts *all* keys matching that quota condition, including disabled ones.

### GET /admin/stats

```json
{"total_keys": 3, "active_keys": 2,
 "keys": [{"id": 1, "provider": "groq", "label": "env-default", "is_enabled": true,
           "api_key_masked": "gsk_...aB2c",
           "rate_limit_remaining_tokens": null, "rate_limit_remaining_requests": null,
           "rate_limit_reset_tokens": null, "rate_limit_reset_requests": null,
           "last_used_at": null}]}
```

This is the raw rate-limit state per key — the most direct way to watch the sweeper NULL
out an exhausted key, or to confirm the header parsing is picking up what you expect.
