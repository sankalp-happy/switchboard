# Configuration

Configuration comes from three places, in decreasing order of how much you should touch them:

1. **Environment variables** read by `core/config.py` into a Pydantic `Settings` model.
2. **Compose-level variables** that never reach Python — passwords and host port mappings.
3. **Hardcoded constants** in the source. Not configurable today; listed here because they
   determine behaviour you may need to reason about.

For how to *generate* the required secrets and get a stack running, see the
[root README](../README.md). This page is the exhaustive list, not the tutorial.

## The Settings model

`core/config.py` defines `Settings(BaseSettings)` with `env_file = ".env"` and
`extra = "allow"`. The module instantiates a singleton `settings` **at import time**, which
matters: anything that imports `core.config` — directly or transitively — freezes the
configuration as it was at that moment. `tests/conftest.py` sets its environment before any
such import for exactly this reason.

`extra = "allow"` means unrecognised variables in `.env` are accepted rather than rejected.
A typo in a variable name will not raise; it will silently leave the real setting at its
default.

| Variable | Type | Default | Required | Effect |
|---|---|---|---|---|
| `ENCRYPTION_KEY` | `str` | `""` | **Yes** | Fernet key for encrypting provider keys at rest. Empty raises `RuntimeError` on the first key operation, with a generation command in the message |
| `ADMIN_TOKENS` | `str` | `""` | **Yes** | Comma-separated. Guards `/admin/*`, `/openapi.json`, `/metrics`; also valid on `/v1/*`. Empty means the gateway **refuses to start** |
| `CLIENT_TOKENS` | `str` | `""` | **Yes** | Comma-separated. Guards `/v1/*` only. Empty means the gateway **refuses to start** |
| `GROQ_API_KEY` | `str` | `""` | No | Seeded into the database at startup if set and no `groq` keys exist yet |
| `GOOGLE_API_KEY` | `str` | `""` | No | Used for embeddings. Unset disables the semantic cache entirely — requests are still served |
| `ANTHROPIC_API_KEY` | `str` | `""` | No | Fallback key for the Anthropic adapter when no database key is supplied |
| `SWITCHBOARD_PROVIDER` | `str` | `"groq"` | No | Default provider when a request omits `provider`. Must be `groq`, `google`, or `anthropic`; anything else raises `ValueError` when `Router` is constructed, at import of `gateway/main.py` |
| `REDIS_URL` | `str` | `redis://localhost:6379/0` | No | Compose overrides this to `redis://:${REDIS_PASSWORD}@redis:6379/0` |
| `SQLITE_DB_PATH` | `str` | `data/switchboard.db` | No | Compose sets `/app/data/switchboard.db`, which is the mount point of the named volume. Parent directories are created on first connect |
| `PORT` | `int` | `8000` | No | Only read by the `__main__` block; the container CMD passes `--port 8000` explicitly |
| `HOST` | `str` | `0.0.0.0` | No | Same — only used when running `python -m gateway.main` |

### Which are load-bearing at import

Three settings are validated before any request is served, so a bad value is a startup
failure rather than a runtime surprise:

- `ADMIN_TOKENS` and `CLIENT_TOKENS` — `gateway/auth.py:96` calls `validate_auth_config()`
  at module import. See [auth.md](auth.md).
- `SWITCHBOARD_PROVIDER` — `gateway/main.py:241` constructs `Router()` at module scope,
  which raises on an unknown provider name.

`ENCRYPTION_KEY` is the exception: it is checked lazily, on the first encrypt or decrypt.
A gateway with no keys in its database will start fine without it and fail on the first
`POST /admin/keys`.

## Compose-level variables

These are consumed by `docker-compose.yml` and never appear in `Settings`.

| Variable | Default | Service | Notes |
|---|---|---|---|
| `REDIS_PASSWORD` | none | redis, gateway | Uses the `:?` form, so Compose **aborts with a named error** if unset rather than starting a Redis with a missing `--requirepass` argument |
| `GRAFANA_ADMIN_PASSWORD` | none | grafana | Also `:?`. Grafana would silently accept an empty value as "no password set" |
| `GATEWAY_PORT` | `8000` | gateway | Host port mapping |
| `ADMIN_UI_PORT` | `3000` | admin-ui | Host port mapping |
| `PROMETHEUS_PORT` | `9090` | prometheus | Host port mapping |
| `GRAFANA_PORT` | `3001` | grafana | Host port mapping |
| `API_URL` | `http://gateway:8000` | admin-ui | Substituted into the nginx config template |
| `IN_DOCKER` | `true` | gateway | Set by the Dockerfile |
| `GF_SECURITY_ADMIN_USER` | `admin` | grafana | |

Redis has no host port mapping by design — see [deployment.md](deployment.md).

`setup.sh` writes all of these into `.env`, remapping any host port that is already in use
and persisting the choice so your URLs stay stable across runs.

## Files, not variables

| Path | Written by | Purpose |
|---|---|---|
| `.env` | `setup.sh` | Everything above. Never committed; excluded from the image by `.dockerignore` |
| `prometheus/scrape_token` | `setup.sh`, mode 600 | The first `ADMIN_TOKENS` entry. Prometheus cannot expand environment variables in its config, so the credential must be a file |
| `VERSION` | manually | Single source of truth for the release number |

If `prometheus/scrape_token` is missing, Docker creates a *directory* at that path and
Prometheus fails to read it. Run `./setup.sh` rather than creating it by hand.

## Hardcoded constants

Not configurable. Change requires a code edit.

| Constant | Value | Defined at | Meaning |
|---|---|---|---|
| `RedisCache.ttl` | `3600` | `cache/redis_client.py:19` | Cache entry lifetime, seconds |
| `RedisCache.similarity_threshold` | `0.9` | `cache/redis_client.py:32` | Minimum cosine similarity to count as a hit |
| `RedisCache.embedding_model` | `gemini-embedding-001` | `cache/redis_client.py:30` | Embedding model |
| `KeyManager.MIN_TOKENS_THRESHOLD` | `100` | `core/key_manager.py:108` | Below this remaining-token count a key is treated as exhausted |
| `SWEEPER_INTERVAL_SECONDS` | `5` | `gateway/main.py:86` | Rate-limit sweeper period |
| `USAGE_CLEANUP_INTERVAL_SECONDS` | `600` | `gateway/main.py:101` | Usage bucket cleanup period |
| usage bucket retention | 25 hours | `core/database.py:160` | How long minute buckets are kept |
| provider HTTP timeout | `30.0` seconds | each `providers/*.py` | Per-request upstream timeout |
| Anthropic `max_tokens` | `1024` | `providers/anthropic_provider.py` | The Anthropic API requires it; there is no field on `ChatCompletionRequest` to source it from |
| duration parse fallback | `60.0` seconds | `core/key_manager.py:98` | Used when a reset-duration header cannot be parsed |
