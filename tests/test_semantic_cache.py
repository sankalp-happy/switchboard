"""
Semantic cache tests, in two tiers.

  test_semantic_cache_threshold_*   unit. Mocks the embedding call and the Redis
                                    client, so the 0.9 cosine gate is exercised
                                    with no credentials and no server. Runs by
                                    default and in CI.

  test_semantic_cache_hit_and_miss  integration. Needs a real GOOGLE_API_KEY and
                                    a reachable Redis. Excluded by pytest.ini's
                                    addopts; run with `pytest -m integration`.

Why both: the integration test proves the real embedding API still behaves the
way the threshold assumes. The unit tests prove OUR similarity logic is right
regardless of what that API returns. Only the second kind can run on a fork PR,
where repository secrets are never available.

    request ──► _get_embedding ──► vector
                                     │
                    ┌────────────────┴────────────────┐
                    │  cosine(vector, cached) >= 0.9  │
                    └────────────────┬────────────────┘
                          yes ───────┴─────── no
                           │                   │
                     return cached        return None
"""

import json
import os
import time
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from cache.redis_client import RedisCache
from core.schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderResult,
    Usage,
)
from gateway import main as gateway_main


def _request(content: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="groq-llama-3",
        messages=[ChatMessage(role="user", content=content)],
    )


def _response(content: str = "42") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="test-123",
        created=int(time.time()),
        model="groq-llama-3",
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
        usage=Usage(),
    )


def _provider_result() -> ProviderResult:
    return ProviderResult(
        response=_response(),
        provider="groq",
        latency_ms=12.0,
        rate_limit_headers={},
    )


# Two unit vectors at a known angle. cos(theta) is exactly the first component of
# `near`/`far` because `base` is the x-axis unit vector, which makes the expected
# similarity readable rather than something you have to trust a comment about.
_BASE = np.array([1.0, 0.0])
_NEAR = np.array([0.95, np.sqrt(1 - 0.95**2)])   # cosine 0.95 -> above the 0.9 gate
_FAR = np.array([0.50, np.sqrt(1 - 0.50**2)])    # cosine 0.50 -> below the 0.9 gate


def _cache_with(stored_embedding, stored_response):
    """
    A RedisCache whose Redis is a mock holding exactly one entry.

    Patches the constructor's genai client away so no GOOGLE_API_KEY is needed,
    then swaps in an AsyncMock for the three Redis calls the read path makes:
    keys(), get(), setex().
    """
    with patch.object(RedisCache, "_get_embedding", return_value=None):
        cache = RedisCache()

    payload = json.dumps(
        {
            "embedding": stored_embedding.tolist(),
            "response": stored_response.model_dump(exclude_unset=True),
            "original_model": "groq-llama-3",
        }
    )

    cache.redis_client = AsyncMock()
    cache.redis_client.keys.return_value = ["nexus:cache:stored"]
    cache.redis_client.get.return_value = payload
    return cache


@pytest.mark.asyncio
async def test_semantic_cache_threshold_admits_near_match():
    """Cosine 0.95 is above the 0.9 gate, so the cached response comes back."""
    cache = _cache_with(_BASE, _response("42"))

    with patch.object(RedisCache, "_get_embedding", return_value=_NEAR):
        cached, similarity = await cache.get_cached_response(_request("near enough"))

    assert cached is not None, "0.95 similarity should clear the 0.9 threshold"
    assert cached.choices[0].message.content == "42"
    assert similarity == pytest.approx(0.95, abs=1e-6)


@pytest.mark.asyncio
async def test_semantic_cache_threshold_rejects_distant_match():
    """Cosine 0.50 is below the gate, so nothing is returned even though an entry exists."""
    cache = _cache_with(_BASE, _response("42"))

    with patch.object(RedisCache, "_get_embedding", return_value=_FAR):
        cached, similarity = await cache.get_cached_response(_request("something else"))

    assert cached is None, "0.50 similarity must not clear the 0.9 threshold"
    assert similarity == pytest.approx(0.50, abs=1e-6)


@pytest.mark.asyncio
async def test_semantic_cache_bypassed_when_embeddings_unavailable():
    """
    No GOOGLE_API_KEY means _get_embedding returns None, and both paths must
    degrade to "no cache" rather than raising. This is the configuration most
    contributors will actually run.
    """
    cache = _cache_with(_BASE, _response("42"))

    with patch.object(RedisCache, "_get_embedding", return_value=None):
        cached, similarity = await cache.get_cached_response(_request("anything"))
        assert cached is None
        assert similarity == -1.0

        # The write path must also no-op rather than store a null embedding.
        await cache.set_cached_response(_request("anything"), _response())
        cache.redis_client.setex.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_cache_empty_prompt_short_circuits():
    """An all-empty message list never reaches the embedding call at all."""
    cache = _cache_with(_BASE, _response("42"))

    request = ChatCompletionRequest(
        model="groq-llama-3",
        messages=[ChatMessage(role="user", content="")],
    )
    cached, similarity = await cache.get_cached_response(request)

    assert cached is None
    assert similarity == -1.0
    cache.redis_client.keys.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_miss_omits_similarity_when_no_comparison_ran(
    client_scope_client, monkeypatch
):
    """A disabled cache must not expose its no-comparison sentinel as a score."""
    monkeypatch.setattr(
        gateway_main.cache,
        "get_cached_response",
        AsyncMock(return_value=(None, -1.0)),
    )
    monkeypatch.setattr(gateway_main.cache, "set_cached_response", AsyncMock())
    monkeypatch.setattr(
        gateway_main.router,
        "route_request",
        AsyncMock(return_value=_provider_result()),
    )

    response = await client_scope_client.post(
        "/v1/chat/completions",
        json=_request("cache disabled").model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    assert "X-Semantic-Similarity" not in response.headers


@pytest.mark.asyncio
async def test_gateway_miss_reports_similarity_when_comparison_ran(
    client_scope_client, monkeypatch
):
    """A real below-threshold comparison remains visible on a cache miss."""
    monkeypatch.setattr(
        gateway_main.cache,
        "get_cached_response",
        AsyncMock(return_value=(None, 0.5)),
    )
    monkeypatch.setattr(gateway_main.cache, "set_cached_response", AsyncMock())
    monkeypatch.setattr(
        gateway_main.router,
        "route_request",
        AsyncMock(return_value=_provider_result()),
    )

    response = await client_scope_client.post(
        "/v1/chat/completions",
        json=_request("below threshold").model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    assert response.headers["X-Semantic-Similarity"] == "0.5000"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"),
    reason="needs a live GOOGLE_API_KEY and a reachable Redis",
)
async def test_semantic_cache_hit_and_miss():
    """
    End-to-end against the real embedding API and a real Redis.

    Verifies the assumption the unit tests above are built on: that the live
    embedding model actually places a trailing-whitespace variant above 0.9 and
    an unrelated prompt below it.
    """
    cache = RedisCache()

    request_1 = _request("What is the meaning of life?")
    await cache.set_cached_response(request_1, _response("42"))

    # Near-identical: differs only by trailing whitespace.
    request_2 = _request("What is the meaning of life? ")
    cached_response_2, _ = await cache.get_cached_response(request_2)
    assert cached_response_2 is not None, "Cache missed on a semantically similar query"
    assert cached_response_2.choices[0].message.content == "42"

    # Unrelated prompt.
    request_3 = _request("How do I bake a chocolate cake?")
    cached_response_3, _ = await cache.get_cached_response(request_3)
    assert cached_response_3 is None, "Cache hit on a semantically distinct query"
