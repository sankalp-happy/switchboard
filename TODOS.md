# TODOS

## Security

### Per-caller rate limiting on `/v1/chat/completions`

**What:** Rate-limit completion requests per client token.

**Why:** Authentication answers "may you call this?" but not "how much?" A leaked or misbehaving client token can still exhaust the entire Groq and Google quota at full speed. The auth PR is what makes this possible at all — before it there was no caller identity to limit against.

**Context:** Substantial counting machinery already exists but is pointed at provider keys rather than callers: `core/key_manager.py` parses provider rate-limit headers into `rate_limit_remaining_tokens` / `rate_limit_remaining_requests`, and `core/database.py` maintains usage buckets queryable via `get_usage_stats(minutes=...)` with a background cleanup at `gateway/main.py:67-78`. Redis is already a dependency and is the natural counter store. Open policy question: one global limit, or per-token limits configured alongside the token itself — the latter argues for eventually moving tokens into the DB (see the virtual-keys idea rejected during the auth review).

**Effort:** M
**Priority:** P2
**Depends on:** Gateway auth PR (caller identity)

### Isolate the semantic cache per caller

**What:** Namespace cache entries by caller identity so one client token cannot receive
another's cached response.

**Why:** `CLIENT_TOKENS` is a comma-separated list by design — `.env.example` describes
handing individual entries to different API callers. But the cache does not know who is
asking. `cache/redis_client.py:55` builds the key from `model + temperature + messages`
with no caller component, and it would not matter if it did: `get_cached_response` at
`:72` runs `KEYS nexus:cache:*` and returns the best cosine match above 0.9 from the
**entire** keyspace, ignoring key structure completely. `gateway/main.py:245` calls it
with only the request — `require_client` has already run at `:234` but the token is never
threaded through. So in any deployment with more than one client token, caller B asking
something semantically close to caller A's prompt receives A's response.

**Context:** The fix is to pass the authenticated caller through from `require_client`
into the cache and scope both the write key and the scan to `nexus:cache:{caller_hash}:*`.
Hash the token rather than storing it. This touches the same two functions as the
keyspace-scan item below, so the two are cheaper done together than separately — and
namespacing the scan shrinks it, which partially addresses that item as a side effect.

Decided on 2026-08-22 to ship v0.2.0 with this documented here rather than fixed, and
without a README disclosure. Revisit before recommending SwitchBoard for any deployment
serving more than one caller.

**Effort:** M
**Priority:** P1
**Depends on:** None

## Performance

### Replace the semantic cache keyspace scan with a vector index

**What:** Stop scanning and downloading the entire Redis keyspace on every completion request.

**Why:** Every cache lookup issues `KEYS nexus:cache:*`, then one `GET` per key, then computes cosine similarity in Python for each. That is O(cache size) network round trips plus O(cache size) numpy operations before the request can even be routed. `KEYS` additionally blocks the whole Redis server for the duration of the scan, so one slow lookup degrades every other caller. Cost grows silently as the cache fills.

**Context:** `cache/redis_client.py:72-90`, and the existing comment at `:70-71` already flags it as an MVP shortcut. Entries are written at `:132` with a 1h TTL, so the keyspace grows with every unique prompt. Note the security link: before the auth PR this was also a DoS amplification vector — an anonymous caller could inflate the keyspace with unique prompts and slow every subsequent request. Authentication closed the attacker-driven half; the performance ceiling for legitimate traffic remains.

Two increments, in order:
1. **Cheap:** swap `KEYS` for `SCAN` (two lines). Removes the server-blocking behaviour. Does not reduce the O(N) fetch-and-compare.
2. **Real:** Redis Stack with a vector index, so similarity search is one indexed query. Requires a new Redis image, an index schema, and a migration path for existing cache entries.

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
retry handling that automatic failover depends on — the reliability claim the README
leads with rests on the three least-tested files in the repo.

**Context:** Measured with `pytest --cov` on 2026-08-22 at commit 2cd90b0. The adapters
all talk to their upstreams over `httpx`, so `httpx.MockTransport` gives real code-path
coverage without mocking the adapters themselves — `tests/test_token_exhaustion.py` is a
worked example of that pattern. Start with the non-200 branches in each adapter
(`groq_provider.py:78`, `google_provider.py:85`, `anthropic_provider.py:106` are the
missing-key guards) and the 429/401 handling in `routing/router.py:127-141`.

Good first contribution: well-scoped, no credentials needed, obvious success criterion.

**Effort:** M
**Priority:** P2
**Depends on:** None

### Verify the OpenAI compatibility contract

**What:** Test `/v1/chat/completions` against what an OpenAI client actually expects.

**Why:** "OpenAI-Compatible API" is the first bullet in the README and the strongest
adoption claim the project makes, and nothing verifies it. Nothing covers streaming,
tool or function calls, error-payload shape, the full `usage` object, or what happens
when a client sends a parameter the gateway does not model. One concrete gap was
already known: `core/schemas.py:12` accepted `stream`, but the provider adapters forced
it to `False`, so a client requesting a stream got a complete response and no error —
a silent contract violation. (Fixed in v0.2.1+: `stream: true` now returns HTTP 400.)

**Context:** The README's Limitations section now scopes the claim honestly, so this is
not urgent, but the gap between "works with the OpenAI SDK" and "implements the OpenAI
API" is where adopter trust is lost. Next increment: decide whether streaming is worth
implementing — it needs SSE handling in every adapter plus a pass-through path that
skips the cache.

**Effort:** S (done) / L (implement streaming)
**Priority:** P2
**Depends on:** None

## Infrastructure

### Run the gateway container as a non-root user

**What:** Add a `USER` directive to the Dockerfile.

**Why:** The gateway container runs as root. Any container security scanner run against a
public repo flags it, and it is the standard hardening step this project has not taken.

**Context:** Deliberately deferred during the open-source release review on 2026-08-22,
for a concrete reason worth recording: `Dockerfile:5` does `mkdir -p /app/data` and
`docker-compose.yml` mounts the named volume `switchboard-data` there. Adding a `USER`
without giving that user ownership of the volume breaks `docker compose up` for anyone
with an existing volume, because named-volume contents keep the ownership they were
created with. The change is a `RUN adduser` plus `chown` plus `USER`, but it needs
testing against both a fresh volume and a pre-existing one before it can land.

Note that `.github/dependabot.yml` does not track the base-image digest pin added in
0.2.0, so that pin needs a manual bump periodically regardless.

**Effort:** S
**Priority:** P3
**Depends on:** None
