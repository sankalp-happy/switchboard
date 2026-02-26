from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from prometheus_fastapi_instrumentator import Instrumentator

from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from core.config import settings
from core.database import init_db
from core.key_manager import key_manager
from core.metrics import CACHE_HITS, CACHE_MISSES, ACTIVE_KEYS
from routing.router import Router
from cache.redis_client import RedisCache
from gateway.admin import admin_router

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("switchboard.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, seed keys, update gauge. Shutdown: nothing special."""
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
    logger.info("Switchboard gateway started.")
    yield
    logger.info("Switchboard gateway shutting down.")


app = FastAPI(
    title="SwitchBoard Gateway",
    description="Multi-provider LLM gateway with key rotation & semantic caching",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cache", "X-Semantic-Similarity"],
)

# Mount admin API
app.include_router(admin_router)

# Prometheus auto-instrumentation (latency histograms, request counts per endpoint)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

router = Router()
cache = RedisCache()


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest, response: Response):
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
    return {"status": "ok", "version": "0.2.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.main:app", host=settings.HOST, port=settings.PORT, reload=True)
