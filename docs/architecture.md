# Architecture

## Components

| Component | Module | Responsibility |
|---|---|---|
| Gateway app | `gateway/main.py` | FastAPI app, lifespan, the completion route, `/health`, docs routes, metrics mount |
| Auth | `gateway/auth.py` | Two-scope bearer authentication, fail-closed at import |
| Admin API | `gateway/admin.py` | Key CRUD, provider summary, usage and stats |
| Router | `routing/router.py` | Provider resolution, key selection, failover, metric emission |
| Key manager | `core/key_manager.py` | Fernet encryption, key selection SQL, rate-limit accounting |
| Providers | `providers/*.py` | One adapter per vendor, translating to and from the unified schema |
| Semantic cache | `cache/redis_client.py` | Embedding generation, cosine similarity lookup, write-back |
| Database | `core/database.py` | aiosqlite singleton, schema, per-key usage buckets |
| Config | `core/config.py` | `pydantic-settings` model, instantiated once at import |
| Metrics | `core/metrics.py` | Prometheus counter/histogram/gauge definitions |

`gateway/` and `routing/` have no `__init__.py` — they are namespace packages. `pytest.ini`
sets `pythonpath = .` so a bare `pytest` can import them.

## Package dependency direction

```
gateway/  ──►  routing/  ──►  providers/
   │              │              │
   └──────────────┴──────────────┴──►  core/   (config, schemas, database, key_manager, metrics)
   │
   └──►  cache/
```

`core/` depends on nothing above it. `gateway/auth.py` exists as a separate module purely
to break a cycle: `gateway/main.py` imports `admin_router` from `gateway/admin.py`, so
`admin.py` cannot import its guard from `main.py`.

## Request flow

`gateway/main.py:245-309`, `chat_completions`.

1. **Authenticate.** `Depends(require_client)` runs before the handler body. Unauthenticated
   callers never reach any provider quota. See [auth.md](auth.md).
2. **Reject streaming.** `request.stream` truthy returns HTTP 400 with an OpenAI-shaped
   error body (`gateway/main.py:258`). This is done in the handler rather than as a Pydantic
   validator so the response carries `error.message` / `error.type` / `error.param`, which
   is what OpenAI clients read — a validator would produce a 422 with Pydantic's own error
   list instead.
3. **Cache lookup.** `cache.get_cached_response(request)` returns
   `(response_or_None, highest_similarity)`. On a hit: increment `CACHE_HITS`, set
   `X-Cache: HIT` and `X-Semantic-Similarity`, return immediately — the provider is never
   called. Any exception here is caught and logged as a warning; a broken cache degrades to
   a miss rather than failing the request.
4. **Route.** On a miss, increment `CACHE_MISSES` and call `router.route_request(request)`.
   See [routing-and-keys.md](routing-and-keys.md).
5. **Write back.** `cache.set_cached_response(...)`, also best-effort — a failure is logged
   and the response is still returned.
6. **Set headers and return.** `X-Cache: MISS`, `X-Provider`, `X-Latency-Ms`, and
   `X-Semantic-Similarity` *only if a comparison actually ran* (`highest_similarity > -1.0`).
   When the cache is disabled or the prompt was empty no similarity was computed, and
   reporting a sentinel value would be a lie.
7. **Errors.** Any exception escaping the router becomes `HTTPException(502)` with the
   underlying message in `detail`.

```
request ──► auth ──► stream check ──► cache ──HIT──► response (X-Cache: HIT)
                                        │
                                       MISS
                                        ▼
                                     router ──► provider ──► cache write ──► response
                                        │
                                      raises
                                        ▼
                                      502 Bad Gateway
```

## Startup flow

`gateway/main.py:46-83`, the `lifespan` context manager.

1. `init_db()` — create the schema if absent, run the lightweight column migration.
2. `key_manager.seed_from_env()` — if `GROQ_API_KEY` is set *and* there are no `groq` rows
   yet, insert it with the label `env-default`. It will not duplicate on restart.
3. Set the `ACTIVE_KEYS` gauge per provider from the enabled keys in the database.
4. **Redis probe.** `await cache.redis_client.ping()` sets the module global `_cache_status`
   to `"ok"` or `"unavailable"`. This exists because `redis.from_url()` is lazy and opens no
   connection at construction, and without `GOOGLE_API_KEY` the cache read path returns
   before it ever touches Redis — so a wrong `REDIS_PASSWORD` or a dead Redis was previously
   completely invisible. The probe **fails loud, not closed**: it logs an error and the
   gateway keeps serving, because the cache is an optimisation, not a security control.
5. Spawn two background tasks (below).

On shutdown both tasks are cancelled.

## Background tasks

| Task | Interval | Defined at | What it does |
|---|---|---|---|
| `_rate_limit_sweeper` | `SWEEPER_INTERVAL_SECONDS = 5` | `gateway/main.py:86-98` | Calls `key_manager.reset_expired_keys()`, which NULLs the remaining-quota columns for enabled keys whose `rate_limit_resets_at` has passed, making them selectable again |
| `_usage_bucket_cleanup` | `USAGE_CLEANUP_INTERVAL_SECONDS = 600` | `gateway/main.py:101-115` | Calls `cleanup_old_buckets()`, deleting `key_usage_buckets` rows older than 25 hours |

Both loops swallow non-cancellation exceptions and log a warning, so a transient database
error does not silently kill the task for the lifetime of the process.

## State stores

- **SQLite** (`SQLITE_DB_PATH`) — provider keys (encrypted), rate-limit state, usage buckets.
  Single-writer; see [data-model.md](data-model.md).
- **Redis** (`REDIS_URL`) — semantic cache entries only, 1-hour TTL. Nothing in Redis is
  authoritative; losing it costs cache hits and nothing else.
- **Process memory** — the parsed token frozensets (`gateway/auth.py`) and `_cache_status`.
  Both are set at startup and only change on restart.
