"""
Tests for provider selection via request.provider.
"""

import os
import tempfile
import time

_tmpdir = tempfile.mkdtemp()
os.environ["SQLITE_DB_PATH"] = os.path.join(_tmpdir, "test_provider_routing.db")

from cryptography.fernet import Fernet

os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["GROQ_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from core.database import init_db
from core.key_manager import key_manager
from core.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatChoice,
    Usage,
    ProviderResult,
)
from routing.router import Router


def _make_request(provider: str | None = None):
    return ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Hello")],
        provider=provider,
    )


def _make_provider_result(provider: str):
    return ProviderResult(
        response=ChatCompletionResponse(
            id="test-1",
            created=int(time.time()),
            model="test-model",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="Hi!"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        ),
        provider=provider,
        latency_ms=150.0,
        rate_limit_headers={},
    )


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    await init_db()
    from core.database import get_db

    db = await get_db()
    await db.execute("DELETE FROM api_keys")
    await db.commit()
    yield


@pytest.mark.asyncio
async def test_router_routes_to_requested_provider():
    with patch("routing.router.GoogleProvider") as MockProvider, patch(
        "routing.router.key_manager.get_available_key",
        new=AsyncMock(return_value=("test_google_key", 1)),
    ), patch(
        "routing.router.key_manager.get_all_keys",
        new=AsyncMock(return_value=[("test_google_key", 1)]),
    ):
        router = Router()
        mock_result = _make_provider_result("google")
        instance = MockProvider.return_value
        instance.generate = AsyncMock(return_value=mock_result)

        result = await router.route_request(_make_request(provider="google"))

        MockProvider.assert_called_once_with(api_key="test_google_key")
        assert result.provider == "google"
        instance.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_rejects_unknown_provider():
    router = Router()
    with pytest.raises(ValueError, match="Unknown provider"):
        await router.route_request(_make_request(provider="unknown"))
