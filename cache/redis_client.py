import json
import hashlib
from typing import Optional, Dict, Any, Tuple
from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from core.config import settings
from google import genai
import numpy as np

import redis.asyncio as redis

SIMILARITY_PRESETS = {
    "strict": 0.95,
    "balanced": 0.9,
    "aggressive": 0.6,
}

_cache_config = {
    "ttl": 3600,
    "similarity_threshold": 0.9,
}


def get_cache_config() -> Dict[str, Any]:
    return dict(_cache_config)


def set_cache_config(ttl: Optional[int] = None, similarity_threshold: Optional[float] = None):
    if ttl is not None:
        _cache_config["ttl"] = ttl
    if similarity_threshold is not None:
        _cache_config["similarity_threshold"] = similarity_threshold


def resolve_similarity_threshold(request_similarity: Optional[str]) -> float:
    if request_similarity is None:
        return _cache_config["similarity_threshold"]
    if request_similarity in SIMILARITY_PRESETS:
        return SIMILARITY_PRESETS[request_similarity]
    try:
        val = float(request_similarity)
        if 0.0 <= val <= 1.0:
            return val
    except (ValueError, TypeError):
        pass
    return _cache_config["similarity_threshold"]


class RedisCache:
    def __init__(self):
        self.redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

        self.genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.embedding_model = "gemini-embedding-001"

    def _extract_text(self, messages) -> str:
        parts = []
        for m in messages:
            if not m.content:
                continue
            if isinstance(m.content, str):
                parts.append(m.content)
            elif isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        parts.append(part)
        return " ".join(parts)

    def _get_embedding(self, text: str) -> Optional[np.ndarray]:
        try:
            result = self.genai_client.models.embed_content(
                model=self.embedding_model,
                contents=text
            )
            return np.array(result.embeddings[0].values)
        except Exception as e:
            print(f"Error generating embedding: {e}")
            return None

    def _generate_key(self, request: ChatCompletionRequest) -> str:
        messages_str = json.dumps([m.model_dump() for m in request.messages], sort_keys=True)
        combined = f"{request.model}_{request.temperature}_{messages_str}"
        return f"nexus:cache:{hashlib.sha256(combined.encode()).hexdigest()}"

    async def get_cached_response(
        self,
        request: ChatCompletionRequest,
        similarity_threshold: Optional[float] = None,
        aggressive_fallback: bool = False,
    ) -> Tuple[Optional[ChatCompletionResponse], float]:
        """Fetch a cached response if available using semantic caching.

        Args:
            request: The chat completion request.
            similarity_threshold: Override threshold for this request.
            aggressive_fallback: If True, return the best match regardless of
                threshold — used when the upstream model call fails.
        """
        messages_text = self._extract_text(request.messages)
        if not messages_text:
            return None, -1.0

        request_embedding = self._get_embedding(messages_text)
        if request_embedding is None:
            return None, -1.0

        keys = await self.redis_client.keys("nexus:cache:*")

        best_match = None
        highest_similarity = -1.0

        for key in keys:
            cached_data = await self.redis_client.get(key)
            if not cached_data:
                continue

            try:
                data_dict = json.loads(cached_data)

                if "embedding" in data_dict:
                    cached_embedding = np.array(data_dict["embedding"])

                    similarity = np.dot(request_embedding, cached_embedding) / (np.linalg.norm(request_embedding) * np.linalg.norm(cached_embedding))

                    if similarity > highest_similarity:
                        highest_similarity = float(similarity)
                        best_match = data_dict
            except Exception as e:
                print(f"Error reading cache for key {key}: {e}")
                continue

        threshold = similarity_threshold if similarity_threshold is not None else _cache_config["similarity_threshold"]

        if best_match:
            if aggressive_fallback and highest_similarity > 0.0:
                print(f"Aggressive fallback! Best similarity: {highest_similarity:.4f}")
                try:
                    if "response" in best_match:
                        return ChatCompletionResponse(**best_match["response"]), highest_similarity
                except Exception as e:
                    print(f"Failed to parse cached response (fallback): {e}")

            if highest_similarity >= threshold:
                print(f"Cache hit! Semantic similarity: {highest_similarity:.4f}")
                try:
                    if "response" in best_match:
                        return ChatCompletionResponse(**best_match["response"]), highest_similarity
                except Exception as e:
                    print(f"Failed to parse cached response: {e}")

        return None, highest_similarity

    async def set_cached_response(self, request: ChatCompletionRequest, response: ChatCompletionResponse):
        messages_text = self._extract_text(request.messages)
        request_embedding = self._get_embedding(messages_text)

        if request_embedding is None:
            print("Failed to generate embedding; bypassing cache storage.")
            return

        key = self._generate_key(request)

        cache_payload = {
            "embedding": request_embedding.tolist(),
            "response": response.model_dump(exclude_unset=True),
            "original_model": request.model
        }

        await self.redis_client.setex(
            key,
            _cache_config["ttl"],
            json.dumps(cache_payload)
        )

    async def get_cache_stats(self) -> Dict[str, Any]:
        keys = await self.redis_client.keys("nexus:cache:*")
        return {
            "entry_count": len(keys),
            "ttl": _cache_config["ttl"],
            "similarity_threshold": _cache_config["similarity_threshold"],
            "similarity_presets": SIMILARITY_PRESETS,
        }

    async def flush_cache(self) -> int:
        keys = await self.redis_client.keys("nexus:cache:*")
        if keys:
            await self.redis_client.delete(*keys)
        return len(keys)