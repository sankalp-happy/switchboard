from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging
import httpx

from prometheus_fastapi_instrumentator import Instrumentator

from core.schemas import ChatCompletionRequest
from core.config import settings
from core.database import init_db, cleanup_old_buckets
from core.key_manager import key_manager
from core.metrics import CACHE_HITS, CACHE_MISSES, ACTIVE_KEYS
from routing.router import Router
from cache.redis_client import RedisCache, resolve_similarity_threshold
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


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, response: Response):
    logger.info(f"Received request for model: {request.model} stream={request.stream}")

    if request.stream:
        try:
            stream_gen = router.route_request_stream(request)
            first_chunk = await stream_gen.__anext__()
        except StopAsyncIteration:
            return StreamingResponse(
                iter([]),
                media_type="text/event-stream",
            )
        except HTTPException:
            raise
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            detail = e.response.text[:500]
            logger.error(f"Stream upstream error {status}: {detail}")
            raise HTTPException(status_code=502, detail=f"Upstream error {status}: {detail}")
        except Exception as e:
            logger.error(f"Stream request failed: {str(e)}")
            raise HTTPException(status_code=502, detail=f"Bad Gateway: {str(e)}")

        async def _stream_with_first():
            yield first_chunk
            try:
                async for chunk in stream_gen:
                    yield chunk
            except Exception as e:
                logger.error(f"Stream mid-flight error: {e}")

        return StreamingResponse(
            _stream_with_first(),
            media_type="text/event-stream",
            headers={
                "X-Cache": "MISS",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    threshold = resolve_similarity_threshold(request.similarity)
    use_model = request.model_use if request.model_use is not None else True
    is_aggressive = request.similarity == "aggressive"

    if not use_model:
        try:
            cached_response, sim = await cache.get_cached_response(
                request, similarity_threshold=threshold,
            )
            if cached_response:
                CACHE_HITS.inc()
                response.headers["X-Cache"] = "HIT"
                response.headers["X-Semantic-Similarity"] = f"{sim:.4f}"
                response.headers["X-Model-Use"] = "false"
                body = cached_response.model_dump()
                body["similarity"] = round(sim, 4)
                return body
        except Exception as e:
            logger.warning(f"Cache lookup failed: {str(e)}")
        raise HTTPException(
            status_code=503,
            detail="model_use=false and no cached response available",
        )

    try:
        logger.info("Routing request to provider.")
        provider_result = await router.route_request(request)

        try:
            await cache.set_cached_response(request, provider_result.response)
        except Exception as e:
            logger.warning(f"Failed to write to cache: {str(e)}")

        body = provider_result.response.model_dump()
        rate_limits = await key_manager.get_key_rate_limits(provider_result.key_id)
        if rate_limits:
            body["rate_limits"] = rate_limits
        response.headers["X-Cache"] = "MISS"
        response.headers["X-Provider"] = provider_result.provider
        response.headers["X-Latency-Ms"] = f"{provider_result.latency_ms:.1f}"
        return body
    except Exception as e:
        logger.error(f"Provider request failed: {str(e)}")

        try:
            fallback_resp, fallback_sim = await cache.get_cached_response(
                request,
                similarity_threshold=threshold,
                aggressive_fallback=is_aggressive,
            )
            if fallback_resp:
                logger.info(f"Cache fallback hit! similarity={fallback_sim:.4f}")
                CACHE_HITS.inc()
                response.headers["X-Cache"] = "FALLBACK"
                response.headers["X-Semantic-Similarity"] = f"{fallback_sim:.4f}"
                response.headers["X-Fallback-Reason"] = "provider-failure"
                body = fallback_resp.model_dump()
                body["similarity"] = round(fallback_sim, 4)
                return body
        except Exception as fallback_err:
            logger.warning(f"Cache fallback also failed: {fallback_err}")

        raise HTTPException(status_code=502, detail=f"Bad Gateway: {str(e)}")


@app.get("/v1/models")
async def list_models():
    keys = await key_manager.list_keys()
    seen = set()
    models = []
    for k in keys:
        if not k.get("is_enabled"):
            continue
        for model_name in k.get("model_cards", []):
            if model_name not in seen:
                seen.add(model_name)
                models.append({
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": k["provider"],
                })
    return {"object": "list", "data": models}


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.2.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.main:app", host=settings.HOST, port=settings.PORT, reload=True)
