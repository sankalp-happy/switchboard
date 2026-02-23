from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import logging

from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from core.config import settings
from routing.router import Router
from cache.redis_client import RedisCache

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("switchboard.gateway")

app = FastAPI(
    title="SwitchBoard Gateway",
    description="Multi-provider LLM gateway (MVP targeting Groq)",
    version="0.1.0"
)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cache", "X-Semantic-Similarity"]
)

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
             response.headers["X-Cache"] = "HIT"
             response.headers["X-Semantic-Similarity"] = f"{highest_similarity:.4f}"
             return cached_response
    except Exception as e:
         logger.warning(f"Failed to fetch from cache: {str(e)}")
         
    # 2. Route Request to Provider
    try:
        logger.info("Cache miss. Routing request to provider.")
        provider_response = await router.route_request(request)
        
        # 3. Store in Cache asynchronously (fire and forget pattern is better suited for a background task but await is fine for MVP)
        try:
             await cache.set_cached_response(request, provider_response)
        except Exception as e:
             logger.warning(f"Failed to write to cache: {str(e)}")
             
        response.headers["X-Cache"] = "MISS"
        if highest_similarity >= -1.0:
            response.headers["X-Semantic-Similarity"] = f"{highest_similarity:.4f}"
        return provider_response
    except Exception as e:
         logger.error(f"Provider request failed: {str(e)}")
         raise HTTPException(status_code=502, detail=f"Bad Gateway: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway.main:app", host=settings.HOST, port=settings.PORT, reload=True)
