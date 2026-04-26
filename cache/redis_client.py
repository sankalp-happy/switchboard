import json
import hashlib
from typing import Optional, Dict, Any, Tuple
from core.schemas import ChatCompletionRequest, ChatCompletionResponse
from core.config import settings
from google import genai
import numpy as np

# Since we use async, typical redis library needs to be aware or we use aioredis (now part of redis-py 4.2+)
import redis.asyncio as redis

class RedisCache:
    def __init__(self):
        self.redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        # Default TTL of 1 hour for MVP
        self.ttl = 3600
        
        # Initialize Google GenAI client for embeddings
        self.genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.embedding_model = "gemini-embedding-001"
# Increased threshold to 0.9 to avoid false positive matches between distinct semantic statements
        self.similarity_threshold = 0.9

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

    def _get_embedding(self, text: str) -> np.ndarray:
        """Helper to generate an embedding for the given text using Gemini."""
        try:
            result = self.genai_client.models.embed_content(
                model=self.embedding_model,
                contents=text
            )
            # embeddings[0].values contains the list of floats
            return np.array(result.embeddings[0].values)
        except Exception as e:
            print(f"Error generating embedding: {e}")
            return None

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

    async def get_cached_response(self, request: ChatCompletionRequest) -> Tuple[Optional[ChatCompletionResponse], float]:
        """Fetch a cached response if available using semantic caching, and return the highest similarity."""
        # 1. Combine messages into a single text for embedding
        messages_text = self._extract_text(request.messages)
        if not messages_text:
            return None, -1.0
            
        request_embedding = self._get_embedding(messages_text)
        if request_embedding is None:
            return None, -1.0

        # 2. Fetch all keys in the cache
        # For a full production system, vector dbs (Redis Stack) or dedicated indices should be used.
        # This is an MVP implementation manually iterating over keys.
        keys = await self.redis_client.keys("nexus:cache:*")
        
        best_match = None
        highest_similarity = -1.0
        
        for key in keys:
            cached_data = await self.redis_client.get(key)
            if not cached_data:
                continue
                
            try:
                data_dict = json.loads(cached_data)
                
                # Extract embedding and calculation similarity
                if "embedding" in data_dict:
                    cached_embedding = np.array(data_dict["embedding"])
                    
                    # Cosine similarity formula: dot(a, b) / (norm(a) * norm(b))
                    similarity = np.dot(request_embedding, cached_embedding) / (np.linalg.norm(request_embedding) * np.linalg.norm(cached_embedding))
                    
                    if similarity > highest_similarity:
                        highest_similarity = float(similarity)
                        best_match = data_dict
            except Exception as e:
                print(f"Error reading cache for key {key}: {e}")
                continue
                
        # 3. If the highest similarity exceeds the threshold, return the cached result
        if best_match and highest_similarity >= self.similarity_threshold:
            print(f"Cache hit! Semantic similarity: {highest_similarity:.4f}")
            # The cached item is a specialized format containing "response", "embedding", and "model"
            try:
                # We expect the payload stored inside the "response" key to map to ChatCompletionResponse
                if "response" in best_match:
                     return ChatCompletionResponse(**best_match["response"]), highest_similarity
            except Exception as e:
                 print(f"Failed to parse cached response: {e}")
                 
        return None, highest_similarity

    async def set_cached_response(self, request: ChatCompletionRequest, response: ChatCompletionResponse):
        """Store a response in the cache along with its embedding."""
        
        messages_text = self._extract_text(request.messages)
        request_embedding = self._get_embedding(messages_text)
        
        if request_embedding is None:
             print("Failed to generate embedding; bypassing cache storage.")
             return
             
        key = self._generate_key(request)
        
        # Package embedding together with response mapping for semantic retrieval
        cache_payload = {
            "embedding": request_embedding.tolist(), # list for JSON serialization
            "response": response.model_dump(exclude_unset=True),
            "original_model": request.model
        }
        
        # Exclude unset to avoid bloated JSON
        await self.redis_client.setex(
            key,
            self.ttl,
            json.dumps(cache_payload)
        )
