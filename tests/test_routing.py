import pytest
from core.schemas import ChatCompletionRequest, ChatMessage
from routing.router import Router

@pytest.mark.asyncio
async def test_router_initializes():
    router = Router()
    assert router.primary_provider is not None

@pytest.mark.asyncio
async def test_router_health():
    router = Router()
    # In a real test, we would mock GroqProvider.
    # For MVP we just check if it returns a boolean
    health = await router.primary_provider.health_check()
    assert isinstance(health, bool)
