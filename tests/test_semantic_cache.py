import pytest
import asyncio
from core.schemas import ChatCompletionRequest, ChatMessage, ChatCompletionResponse
from cache.redis_client import RedisCache

# Pytest fixture to handle async tests
@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
async def test_semantic_cache_hit_and_miss():
    cache = RedisCache()
    
    # 1. Create a mock request and response
    request_1 = ChatCompletionRequest(
        model="groq-llama-3",
        messages=[ChatMessage(role="user", content="What is the meaning of life?")]
    )
    
    import time
    from core.schemas import Usage, ChatChoice

    response_1 = ChatCompletionResponse(
        id="test-123",
        created=int(time.time()),
        model="groq-llama-3",
        choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="42"), finish_reason="stop")],
        usage=Usage()
    )
    
    # 2. Store the response in the cache
    # This will generate the embedding for "What is the meaning of life?" and store it.
    await cache.set_cached_response(request_1, response_1)
    
    # 3. Create a semantically similar request
    # Since threshold is 0.9, we need near-identical queries (e.g., casing/punctuation diff)
    request_2 = ChatCompletionRequest(
        model="groq-llama-3",
        messages=[ChatMessage(role="user", content="What is the meaning of life? ")]
    )
    
    # 4. Attempt to fetch from cache. It should hit due to high semantic similarity.
    cached_response_2, similarity_2 = await cache.get_cached_response(request_2)
    assert cached_response_2 is not None, "Cache missed on a semantically similar query"
    assert cached_response_2.choices[0].message.content == "42", "Returned incorrect response"
    
    # 5. Create a semantically different request
    request_3 = ChatCompletionRequest(
        model="groq-llama-3",
        messages=[ChatMessage(role="user", content="How do I bake a chocolate cake?")]
    )
    
    # 6. Attempt to fetch from cache. It should miss due to low semantic similarity.
    cached_response_3, similarity_3 = await cache.get_cached_response(request_3)
    assert cached_response_3 is None, "Cache hit on a semantically distinct query"
