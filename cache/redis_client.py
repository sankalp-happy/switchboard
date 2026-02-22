import json
import hashlib
from typing import Optional, Dict, Any
from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from core.config import settings

# Since we use async, typical redis library needs to be aware or we use aioredis (now part of redis-py 4.2+)
import redis.asyncio as redis

class RedisCache:
    def __init__(self):
        self.redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        # Default TTL of 1 hour for MVP
        self.ttl = 3600

    def _generate_key(self, request: ChatCompletionRequest) -> str:
        """
        Generate a unique key based on the exact match of the prompt sequence and model.
        In MVP this is a simple exact match hash.
        """
        # Serialize messages to a stable string
        messages_str = json.dumps([m.model_dump() for m in request.messages], sort_keys=True)
        # Include model and temperature to ensure distinct caches for different generation params
        combined = f"{request.model}_{request.temperature}_{messages_str}"
        return f"nexus:cache:{hashlib.sha256(combined.encode()).hexdigest()}"

    async def get_cached_response(self, request: ChatCompletionRequest) -> Optional[ChatCompletionResponse]:
        """Fetch a cached response if available."""
        key = self._generate_key(request)
        cached_data = await self.redis_client.get(key)
        
        if cached_data:
            try:
                data_dict = json.loads(cached_data)
                return ChatCompletionResponse(**data_dict)
            except Exception as e:
                # If cache is corrupted or schema changed, ignore it
                print(f"Error reading cache for key {key}: {e}")
                return None
        return None

    async def set_cached_response(self, request: ChatCompletionRequest, response: ChatCompletionResponse):
        """Store a response in the cache."""
        key = self._generate_key(request)
        # Exclude unset to avoid bloated JSON
        await self.redis_client.setex(
            key,
            self.ttl,
            json.dumps(response.model_dump(exclude_unset=True))
        )
