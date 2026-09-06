# Semantic cache

`cache/redis_client.py`, class `RedisCache`. One instance is constructed at module scope in
`gateway/main.py:242`.

The cache matches on **meaning**, not on exact text. Two differently-worded prompts that
embed close enough together return the same cached response.

## Constants

| Constant | Value | Meaning |
|---|---|---|
| `embedding_model` | `gemini-embedding-001` | Google Gemini embedding model |
| `similarity_threshold` | `0.9` | Minimum cosine similarity to count as a hit |
| `ttl` | `3600` | Entry lifetime in seconds |
| key prefix | `nexus:cache:` | Historical name; predates the project's rename |

The threshold was raised to 0.9 specifically to avoid false-positive matches between
distinct semantic statements. Lowering it trades correctness for hit rate, and the failure
mode is silent: a caller receives a plausible answer to a question they did not ask.

## Read path

`get_cached_response(request)` returns a tuple `(response_or_None, highest_similarity)`.

1. Join every non-empty `message.content` with spaces into one string. If the result is
   empty, return `(None, -1.0)` — nothing to embed.
2. Embed it. If embedding fails or is disabled, return `(None, -1.0)`.
3. `KEYS nexus:cache:*` to list every entry.
4. `GET` each key, parse the JSON, and compute cosine similarity between the request
   embedding and the stored one:
   `dot(a, b) / (norm(a) * norm(b))`, in numpy.
5. Track the single best match and its score across all entries.
6. If the best score is `>= 0.9`, parse `best_match["response"]` into a
   `ChatCompletionResponse` and return it with the score. Otherwise return
   `(None, highest_similarity)`.

Individual malformed entries are logged and skipped rather than failing the lookup.

The `-1.0` sentinel is load-bearing. `gateway/main.py` only sets the
`X-Semantic-Similarity` header on a miss when `highest_similarity > -1.0`, i.e. when a
comparison actually happened. Reporting `-1.0000` to a caller when the cache was disabled or
the prompt was empty would be a lie about a measurement that was never taken.

## Write path

`set_cached_response(request, response)` embeds the same joined message text, and if that
succeeds writes with `SETEX` at the 1-hour TTL:

```json
{
  "embedding": [0.013, -0.041, "..."],
  "response":  { "...": "the full ChatCompletionResponse, model_dump(exclude_unset=True)" },
  "original_model": "llama-3.1-8b-instant"
}
```

A failed embedding logs a warning and skips the write entirely.

## The cache key

`_generate_key` hashes `f"{model}_{temperature}_{messages_json}"` with SHA-256:

```
nexus:cache:<sha256 hex>
```

Messages are serialised with `sort_keys=True` for stability. Model and temperature are
included so that different generation parameters do not share a cache entry.

**But the key structure does not actually gate retrieval.** The read path scans
`nexus:cache:*` and returns the best cosine match from the *entire* keyspace, ignoring key
structure completely. So a cached entry written for one model can be returned for a request
naming a different model, as long as the prompts embed close enough. The key's only real
function is deduplicating writes for identical requests.

## Disabling the cache

Leave `GOOGLE_API_KEY` unset. Then:

- `genai.Client()` is never constructed — important, because it raises without a key, and
  this runs at import time, which would take the whole gateway down.
- `_get_embedding` returns `None` immediately.
- Both the read and write paths short-circuit on that `None`.
- Every response reports `X-Cache: MISS` with no `X-Semantic-Similarity` header.
- Nothing is sent to Google and nothing is written to Redis.

A startup warning is logged: `GOOGLE_API_KEY is not set; semantic cache disabled (requests
still served).`

Note that `/health` still reports `cache: "ok"` in this state, because that field tracks
Redis reachability, not whether semantic caching is switched on.

## Failure behaviour

The cache is an optimisation, never a hard dependency. Every failure mode degrades to a
miss:

| Failure | Result |
|---|---|
| `GOOGLE_API_KEY` unset | Cache disabled, warning at startup |
| Embedding API error | Logged, treated as a miss (read) or skipped (write) |
| Redis unreachable | `gateway/main.py` catches the exception, logs a warning, continues to the router. The startup probe sets `/health` `cache` to `"unavailable"` |
| Malformed cache entry | That entry is skipped; the scan continues |
| Cached response fails to parse | Logged, treated as a miss |

## Data handling

**Semantic caching sends prompt text to Google.** When `GOOGLE_API_KEY` is set, every
completion request has its message text sent to Google's `gemini-embedding-001` endpoint on
both the read and the write path — and it happens regardless of which provider ultimately
serves the completion. A request routed to Groq or Anthropic still has its prompt embedded
by Google.

Cached prompts and responses live in Redis, unencrypted at rest, for one hour. Redis is not
published to the host by Docker Compose and is password-protected.

## Known limitations

Both are tracked in [TODOS.md](TODOS.md) with full context.

**No caller isolation (P1).** The cache key contains no caller component, and the scan
ignores key structure anyway. In any deployment with more than one `CLIENT_TOKENS` entry,
caller B asking something semantically close to caller A's prompt receives A's response.
The fix is to thread the authenticated caller through from `require_client` and scope both
the write key and the scan to `nexus:cache:{caller_hash}:*`.

**The lookup scans the whole keyspace (P2).** `KEYS` plus one `GET` per entry plus a numpy
cosine per entry, on every request. That is O(cache size) round trips and O(cache size)
computation before routing can even begin, and `KEYS` blocks the entire Redis server for the
duration of the scan. Cost grows silently as the cache fills. Two increments are proposed:
swap `KEYS` for `SCAN` (removes the server-blocking behaviour, two lines), then move to
Redis Stack with a real vector index (removes the O(N) fetch-and-compare).

Namespacing the scan per caller would shrink it, so the two items are cheaper done together.
