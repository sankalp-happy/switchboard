from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.openapi.utils import get_openapi
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging

from prometheus_fastapi_instrumentator import Instrumentator

from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from core.config import settings
from core.database import init_db, cleanup_old_buckets
from core.key_manager import key_manager
from core.metrics import CACHE_HITS, CACHE_MISSES, ACTIVE_KEYS
from routing.router import Router
from cache.redis_client import RedisCache
from gateway.admin import admin_router
from gateway.auth import require_admin, require_client

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("switchboard.gateway")

# The VERSION file is the single source of truth for the release number: it is
# what /health reports, what CHANGELOG.md documents, and what CI asserts the
# README and docs site agree with. Previously this was a string literal here,
# which is exactly the kind of thing that silently drifts one release later.
# Falls back rather than raising — a missing VERSION file must not take the
# gateway down over a cosmetic field.
try:
    VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
except OSError:  # pragma: no cover - only reachable on a broken install
    VERSION = "unknown"

# Reported by /health. Set once at startup by the Redis probe below.
#
# Why this exists: RedisCache uses redis.from_url(), which is lazy and opens no
# connection at construction, and without GOOGLE_API_KEY the cache read path
# returns before it ever touches Redis. A wrong REDIS_PASSWORD or a dead Redis
# was therefore completely invisible — /health said ok, requests were served,
# and the semantic cache was simply off. The probe makes that state loud.
_cache_status = "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, seed keys, probe Redis, update gauge."""
    await init_db()
    await key_manager.seed_from_env()
    # Update active keys gauge
    keys = await key_manager.list_keys()
    providers_seen: dict = {}
    for k in keys:
        prov = k["provider"]
        if k["is_enabled"]:
            providers_seen[prov] = providers_seen.get(prov, 0) + 1
    for prov, count in providers_seen.items():
        ACTIVE_KEYS.labels(provider=prov).set(count)
    # Probe Redis. Fail LOUD, not closed: the cache is an optimisation, not a
    # security control, so a Redis blip should cost cache hits rather than take
    # the whole gateway down. But it must never fail silently again.
    global _cache_status
    try:
        await cache.redis_client.ping()
        _cache_status = "ok"
        logger.info("Redis reachable; semantic cache active.")
    except Exception as e:
        _cache_status = "unavailable"
        logger.error(
            "Redis unreachable (%s): %s. Serving requests WITHOUT the semantic "
            "cache. Check REDIS_PASSWORD and REDIS_URL.",
            type(e).__name__, e,
        )
    # Start background sweeper for expired rate-limit keys
    sweeper_task = asyncio.create_task(_rate_limit_sweeper())
    # Start background cleanup for old usage buckets
    cleanup_task = asyncio.create_task(_usage_bucket_cleanup())
    logger.info("Switchboard gateway started (rate-limit sweeper active).")
    yield
    sweeper_task.cancel()
    cleanup_task.cancel()
    logger.info("Switchboard gateway shutting down.")


SWEEPER_INTERVAL_SECONDS = 5


async def _rate_limit_sweeper():
    """Periodically reset keys whose Groq rate-limit window has expired."""
    while True:
        try:
            await asyncio.sleep(SWEEPER_INTERVAL_SECONDS)
            await key_manager.reset_expired_keys()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Rate-limit sweeper error: {e}")


USAGE_CLEANUP_INTERVAL_SECONDS = 600  # every 10 minutes


async def _usage_bucket_cleanup():
    """Periodically delete usage buckets older than 25 hours."""
    while True:
        try:
            await asyncio.sleep(USAGE_CLEANUP_INTERVAL_SECONDS)
            deleted = await cleanup_old_buckets()
            if deleted:
                logger.info(f"Cleaned up {deleted} old usage bucket(s).")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Usage bucket cleanup error: {e}")


app = FastAPI(
    title="SwitchBoard Gateway",
    description="Multi-provider LLM gateway with key rotation & semantic caching",
    version="0.2.0",
    lifespan=lifespan,
    # The auto-mounted docs handed the complete API schema to anyone who asked,
    # including the exact request body for adding a provider key. Disabled here
    # and re-added below with the schema behind the admin guard.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# No CORS middleware, deliberately.
#
# nginx (vis/default.conf.template) serves the dashboard AND reverse-proxies
# /admin/, /v1/, /health, /metrics to the gateway, and vis/index.html sets
# API_BASE = ''. Every dashboard call is therefore same-origin, and browsers
# never apply CORS to same-origin requests — so this middleware was protecting
# nothing while allow_origins=["*"] with allow_credentials=True advertised the
# opposite. (That pairing is rejected by browsers anyway, which is the only
# reason it was not worse.)
#
# If the dashboard is ever served from a different host than the gateway, add
# CORSMiddleware back with an explicit origin list. Never a wildcard, and never
# a wildcard together with credentials.

# Mount admin API
app.include_router(admin_router)


# ---- OpenAPI docs -------------------------------------------------------
#
#   GET /openapi.json  ──► require_admin ──► the schema  (the actual secret)
#   GET /docs          ──► static shell, zero information
#                            │
#                            └─► JS prompts for the token, stores it, and
#                                attaches it when fetching /openapi.json
#
# The shell is unauthenticated on purpose: a browser navigating to /docs cannot
# send an Authorization header, so guarding the page itself would make it
# unloadable. The page contains no data — everything of value is behind the
# guarded schema route. /redoc is not re-added because ReDoc has no clean way
# to attach an auth header to its schema fetch.

_SWAGGER_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"

_DOCS_HTML = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>SwitchBoard Gateway - API docs</title>
  <link rel="stylesheet" href="{_SWAGGER_CDN}/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{_SWAGGER_CDN}/swagger-ui-bundle.js"></script>
  <script>
    var KEY = 'switchboard_admin_token';
    var token = localStorage.getItem(KEY);
    if (!token) {{
      token = window.prompt('Admin token (ADMIN_TOKENS) to load the API schema:');
      if (token) {{ localStorage.setItem(KEY, token); }}
    }}
    SwaggerUIBundle({{
      url: '/openapi.json',
      dom_id: '#swagger-ui',
      requestInterceptor: function (req) {{
        if (token) {{ req.headers['Authorization'] = 'Bearer ' + token; }}
        return req;
      }},
      onComplete: function () {{
        if (!token) {{
          document.getElementById('swagger-ui').innerHTML =
            '<p style="font-family:sans-serif;padding:2rem">' +
            'An admin token is required to load the API schema. Reload to retry.</p>';
        }}
      }},
    }});
  </script>
</body>
</html>"""


@app.get("/openapi.json", include_in_schema=False,
         dependencies=[Depends(require_admin)])
async def openapi_schema():
    """The API schema. Admin token required — this is the enumeration surface."""
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )


@app.get("/docs", include_in_schema=False)
async def swagger_ui():
    """Static Swagger shell. Carries no data; prompts for the token in-browser."""
    return HTMLResponse(_DOCS_HTML)

# Prometheus auto-instrumentation (latency histograms, request counts per endpoint)
#
# Behind require_admin. The page is not "just uptime": it carries per-endpoint
# request counts, latency histograms and the ACTIVE_KEYS gauge per provider —
# enough to read off traffic volume, which providers are live, and how many keys
# are held. The gateway port is published to the host on purpose (that is the
# product), so an unauthenticated /metrics was readable by anything that could
# route here.
#
# expose() forwards **kwargs to app.get(), which is how the guard attaches
# without wrapping the instrumentator's own handler.
#
# Prometheus authenticates its scrape with the first ADMIN_TOKENS entry, read
# from prometheus/scrape_token (see prometheus/prometheus.yml).
# ponytail: reuses the admin scope rather than adding a metrics-only one, so
# the Prometheus container holds a full admin credential. Add a METRICS_TOKENS
# scope in gateway/auth.py if Prometheus ever stops being as trusted as the
# gateway itself.
Instrumentator().instrument(app).expose(
    app, endpoint="/metrics", dependencies=[Depends(require_admin)]
)

router = Router()
cache = RedisCache()


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse,
          dependencies=[Depends(require_client)])
async def chat_completions(request: ChatCompletionRequest, response: Response):
    if request.stream:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Streaming is not supported in this version of SwitchBoard.",
                    "type": "unsupported_parameter",
                    "param": "stream",
                }
            },
        )
    # require_client has already run: the caller holds a CLIENT_TOKENS entry
    # (or an ADMIN_TOKENS one, which is a superset). Unauthenticated callers
    # never reach this body, so provider quota can no longer be spent by
    # anyone who can route to the port.
    logger.info(f"Received request for model: {request.model}")

    # 1. Check Cache
    highest_similarity = -1.0
    try:
        cached_response, highest_similarity = await cache.get_cached_response(request)
        if cached_response:
            logger.info("Cache hit!")
            CACHE_HITS.inc()
            response.headers["X-Cache"] = "HIT"
            response.headers["X-Semantic-Similarity"] = f"{highest_similarity:.4f}"
            return cached_response
    except Exception as e:
        logger.warning(f"Failed to fetch from cache: {str(e)}")

    CACHE_MISSES.inc()

    # 2. Route Request to Provider (with automatic key rotation)
    try:
        logger.info("Cache miss. Routing request to provider.")
        provider_result = await router.route_request(request)

        # 3. Store in Cache
        try:
            await cache.set_cached_response(request, provider_result.response)
        except Exception as e:
            logger.warning(f"Failed to write to cache: {str(e)}")

        response.headers["X-Cache"] = "MISS"
        response.headers["X-Provider"] = provider_result.provider
        response.headers["X-Latency-Ms"] = f"{provider_result.latency_ms:.1f}"
        if highest_similarity >= -1.0:
            response.headers["X-Semantic-Similarity"] = f"{highest_similarity:.4f}"
        return provider_result.response
    except Exception as e:
        logger.error(f"Provider request failed: {str(e)}")
        raise HTTPException(status_code=502, detail=f"Bad Gateway: {str(e)}")


@app.get("/health")
async def health_check():
    """
    Intentionally the ONLY unauthenticated route. Docker healthchecks and
    orchestration depend on it.

    Always returns HTTP 200 so a degraded cache does not fail the healthcheck.
    The `cache` field is how a human or a monitor sees that Redis is down —
    previously that state was completely invisible.
    """
    return {"status": "ok", "version": VERSION, "cache": _cache_status}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.main:app", host=settings.HOST, port=settings.PORT, reload=True)
