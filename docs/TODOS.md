# TODOS

Known gaps, each with enough context to pick one up cold. These are **tracked**, not
undiscovered — the [root README](../README.md) links here from its Limitations section, and
so does [SECURITY.md](../SECURITY.md).

Every item below states what it is, why it matters, and where in the code it lives. Code
citations are `file:line` against the current tree; the symbol name beside them is the
durable part.

## Summary

| Item | Area | Priority | Effort | Reference |
|---|---|---|---|---|
| [Isolate the semantic cache per caller](#isolate-the-semantic-cache-per-caller) | Security | **P1** | M | [caching.md](caching.md) |
| [Per-caller rate limiting](#per-caller-rate-limiting-on-v1chatcompletions) | Security | P2 | M | [auth.md](auth.md) |
| [Replace the cache keyspace scan](#replace-the-semantic-cache-keyspace-scan-with-a-vector-index) | Performance | P2 | S then L | [caching.md](caching.md) |
| [Raise adapter and gateway coverage](#raise-provider-adapter-and-gateway-coverage) | Testing | P2 | M | [development.md](development.md) |
| [Verify the OpenAI contract](#verify-the-openai-compatibility-contract) | Testing | P2 | S done / L streaming | [api-reference.md](api-reference.md) |
| [Run the container as non-root](#run-the-gateway-container-as-a-non-root-user) | Infrastructure | P3 | S | [deployment.md](deployment.md) |

Good first contributions: **raise adapter coverage** (well-scoped, no credentials needed,
obvious success criterion) and **step 1 of the keyspace scan** (`KEYS` to `SCAN`, two lines).

---

## Security

### Isolate the semantic cache per caller

**What:** Namespace cache entries by caller identity so one client token cannot receive
another's cached response.

**Why:** `CLIENT_TOKENS` is a comma-separated list by design — `.env.example` describes
handing individual entries to different API callers. But the cache does not know who is
asking. `cache/redis_client.py:50` builds the key from `model + temperature + messages`
with no caller component, and it would not matter if it did: `get_cached_response` at
`:61` runs `KEYS nexus:cache:*` at `:75` and returns the best cosine match above 0.9 from
the **entire** keyspace, ignoring key structure completely. `gateway/main.py:278` calls it
with only the request — `require_client` has already run at `:246` but the token is never
threaded through. So in any deployment with more than one client token, caller B asking
something semantically close to caller A's prompt receives A's response.

**Context:** The fix is to pass the authenticated caller through from `require_client`
into the cache and scope both the write key and the scan to `nexus:cache:{caller_hash}:*`.
Hash the token rather than storing it. This touches the same two functions as the
keyspace-scan item below, so the two are cheaper done together than separately — and
namespacing the scan shrinks it, which partially addresses that item as a side effect.

Note that `require_client` currently returns `None`; threading caller identity through
means changing it to return the matched token (or a hash of it) and taking it as a handler
argument rather than a bare `dependencies=[...]` entry.

Decided on 2026-08-22 to ship v0.2.0 with this documented here rather than fixed, and
without a README disclosure. Revisit before recommending SwitchBoard for any deployment
serving more than one caller.

**Effort:** M
**Priority:** P1
**Depends on:** None

### Per-caller rate limiting on `/v1/chat/completions`

**What:** Rate-limit completion requests per client token.

**Why:** Authentication answers "may you call this?" but not "how much?" A leaked or
misbehaving client token can still exhaust the entire Groq and Google quota at full speed.
The auth PR is what makes this possible at all — before it there was no caller identity to
limit against.

**Context:** Substantial counting machinery already exists but is pointed at provider keys
rather than callers: `core/key_manager.py` parses provider rate-limit headers into
`rate_limit_remaining_tokens` / `rate_limit_remaining_requests`, and `core/database.py`
maintains usage buckets queryable via `get_usage_stats(minutes=...)` with a background
cleanup task at `gateway/main.py:104-115`. Redis is already a dependency and is the natural
counter store.

Open policy question: one global limit, or per-token limits configured alongside the token
itself — the latter argues for eventually moving tokens into the DB (see the virtual-keys
idea rejected during the auth review). Note that tokens are currently parsed once at import
into frozensets (`gateway/auth.py:61-62`), so per-token configuration would also mean giving
up "restart to change a token".

**Effort:** M
**Priority:** P2
**Depends on:** Gateway auth PR (caller identity)

## Performance

### Replace the semantic cache keyspace scan with a vector index

**What:** Stop scanning and downloading the entire Redis keyspace on every completion
request.

**Why:** Every cache lookup issues `KEYS nexus:cache:*`, then one `GET` per key, then
computes cosine similarity in Python for each. That is O(cache size) network round trips
plus O(cache size) numpy operations before the request can even be routed. `KEYS`
additionally blocks the whole Redis server for the duration of the scan, so one slow lookup
degrades every other caller. Cost grows silently as the cache fills.

**Context:** `cache/redis_client.py:75-100`, and the existing comment at `:73-74` already
flags it as an MVP shortcut. Entries are written at `:135` with a 1h TTL, so the keyspace
grows with every unique prompt.

Note the security link: before the auth PR this was also a DoS amplification vector — an
anonymous caller could inflate the keyspace with unique prompts and slow every subsequent
request. Authentication closed the attacker-driven half; the performance ceiling for
legitimate traffic remains.

Two increments, in order:

1. **Cheap:** swap `KEYS` for `SCAN` (two lines). Removes the server-blocking behaviour.
   Does not reduce the O(N) fetch-and-compare.
2. **Real:** Redis Stack with a vector index, so similarity search is one indexed query.
   Requires a new Redis image, an index schema, and a migration path for existing cache
   entries.

Step 2 changes the stored payload, so it interacts with the caller-isolation item above —
if both are planned, design the key scheme once.

**Effort:** S (step 1) / L (step 2)
**Priority:** P2
**Depends on:** None

## Testing

### Raise provider adapter and gateway coverage

**What:** Add tests for the three provider adapters and the completions endpoint.

**Why:** Coverage is uneven in exactly the wrong place. `gateway/auth.py` is at 100% and
`core/` sits between 82% and 100%, but `providers/anthropic_provider.py` is at 24%,
`providers/google_provider.py` and `providers/groq_provider.py` are at 30%, and
`gateway/main.py` is at 47%. Overall 68%. The untested surface is the provider error and
retry handling that automatic failover depends on — the reliability claim the README leads
with rests on the three least-tested files in the repo.

**Context:** Measured with `pytest --cov` on 2026-08-22 at commit 2cd90b0. The adapters all
talk to their upstreams over `httpx`, so `httpx.MockTransport` gives real code-path coverage
without mocking the adapters themselves — `tests/test_token_exhaustion.py` is a worked
example of that pattern, and `tests/test_provider_routing.py` shows how to patch a class in
the router registry.

Start with the missing-key guards in each adapter (`providers/groq_provider.py:75`,
`providers/google_provider.py:84`, `providers/anthropic_provider.py:106`) and the
429/401/5xx handling in `routing/router.py:126-145`.

Worth covering while you are in there, because it is untested and non-obvious: the Anthropic
adapter's `_build_payload` translation — the system-message hoist, the hardcoded
`max_tokens: 1024`, and the fact that only the first content block of the response is read.

Good first contribution: well-scoped, no credentials needed, obvious success criterion.

**Effort:** M
**Priority:** P2
**Depends on:** None

### Verify the OpenAI compatibility contract

**What:** Test `/v1/chat/completions` against what an OpenAI client actually expects.

**Why:** "OpenAI-Compatible API" is the first bullet in the README and the strongest
adoption claim the project makes, and nothing verifies it. Nothing covers streaming, tool or
function calls, error-payload shape, the full `usage` object, or what happens when a client
sends a parameter the gateway does not model — and `core/config.py` sets `extra = "allow"`
on settings, while `ChatCompletionRequest` silently drops unmodelled fields.

One concrete gap was already known: `core/schemas.py:12` accepted `stream`, but the provider
adapters forced it to `False`, so a client requesting a stream got a complete response and no
error — a silent contract violation. (Fixed on `main`, unreleased: `stream: true` now returns
HTTP 400 in the OpenAI error shape; see `tests/test_stream_rejection.py` and
[api-reference.md](api-reference.md).)

**Context:** The README's Limitations section now scopes the claim honestly, so this is not
urgent, but the gap between "works with the OpenAI SDK" and "implements the OpenAI API" is
where adopter trust is lost.

Next increment: decide whether streaming is worth implementing — it needs SSE handling in
every adapter plus a pass-through path that skips the cache entirely, since there is no
complete response object to embed or store until the stream ends.

**Effort:** S (done) / L (implement streaming)
**Priority:** P2
**Depends on:** None

## Infrastructure

### Run the gateway container as a non-root user

**What:** Add a `USER` directive to the Dockerfile.

**Why:** The gateway container runs as root. Any container security scanner run against a
public repo flags it, and it is the standard hardening step this project has not taken.

**Context:** Deliberately deferred during the open-source release review on 2026-08-22, for
a concrete reason worth recording: `Dockerfile:8` does `mkdir -p /app/data` and
`docker-compose.yml` mounts the named volume `switchboard-data` there. Adding a `USER`
without giving that user ownership of the volume breaks `docker compose up` for anyone with
an existing volume, because named-volume contents keep the ownership they were created with.
The change is a `RUN adduser` plus `chown` plus `USER`, but it needs testing against both a
fresh volume and a pre-existing one before it can land.

Note that `.github/dependabot.yml` does not track the base-image digest pin added in 0.2.0,
so that pin needs a manual bump periodically regardless.

**Effort:** S
**Priority:** P3
**Depends on:** None
