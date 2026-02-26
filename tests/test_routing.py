"""
Tests for the Router — key selection, key switching on 429, all-keys-exhausted.
Uses mocked provider calls and in-memory SQLite.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp()
os.environ["SQLITE_DB_PATH"] = os.path.join(_tmpdir, "test_routing.db")

from cryptography.fernet import Fernet

os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["GROQ_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import time
import httpx

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


def _make_request():
    return ChatCompletionRequest(
        model="llama-3.1-8b-instant",
        messages=[ChatMessage(role="user", content="Hello")],
    )


def _make_provider_result():
    return ProviderResult(
        response=ChatCompletionResponse(
            id="test-1",
            created=int(time.time()),
            model="llama-3.1-8b-instant",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="Hi!"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        ),
        provider="groq",
        latency_ms=150.0,
        rate_limit_headers={
            "x-ratelimit-remaining-tokens": "4900",
            "x-ratelimit-remaining-requests": "29",
        },
    )


@pytest_asyncio.fixture(autouse=True)
async def setup():
    await init_db()
    # Clean between tests
    from core.database import get_db

    db = await get_db()
    await db.execute("DELETE FROM api_keys")
    await db.commit()
    await db.close()
    yield


@pytest.mark.asyncio
async def test_router_picks_key_and_calls_provider():
    """Router should get a key from KeyManager and call the provider."""
    await key_manager.add_key("groq", "gsk_test_key_1", "test1")

    router = Router()
    mock_result = _make_provider_result()

    with patch("routing.router.GroqProvider") as MockProvider:
        instance = MockProvider.return_value
        instance.generate = AsyncMock(return_value=mock_result)

        result = await router.route_request(_make_request())

        MockProvider.assert_called_once_with(api_key="gsk_test_key_1")
        assert result.response.choices[0].message.content == "Hi!"


@pytest.mark.asyncio
async def test_router_switches_key_on_429():
    """Router should mark key as exhausted on 429 and try next key."""
    await key_manager.add_key("groq", "gsk_key_a", "a")
    await key_manager.add_key("groq", "gsk_key_b", "b")

    router = Router()
    mock_result = _make_provider_result()

    # First call raises 429, second succeeds
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    mock_response_429.request = MagicMock()
    error_429 = httpx.HTTPStatusError(
        "rate limited", request=mock_response_429.request, response=mock_response_429
    )

    call_count = 0

    async def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise error_429
        return mock_result

    with patch("routing.router.GroqProvider") as MockProvider:
        instance = MockProvider.return_value
        instance.generate = AsyncMock(side_effect=side_effect)

        result = await router.route_request(_make_request())
        assert result.response.choices[0].message.content == "Hi!"
        assert call_count == 2  # tried twice


@pytest.mark.asyncio
async def test_router_raises_when_all_keys_exhausted():
    """Router should raise when every key returns 429."""
    await key_manager.add_key("groq", "gsk_only_key", "only")

    router = Router()

    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    mock_response_429.request = MagicMock()
    error_429 = httpx.HTTPStatusError(
        "rate limited", request=mock_response_429.request, response=mock_response_429
    )

    with patch("routing.router.GroqProvider") as MockProvider:
        instance = MockProvider.return_value
        instance.generate = AsyncMock(side_effect=error_429)

        with pytest.raises(Exception, match="All API keys exhausted"):
            await router.route_request(_make_request())
