"""`stream: true` must fail loudly.

The gateway accepts a `stream` field it does not implement. It used to force it
back to False in the adapters and return a complete response with a 200, which
left the caller no way to know its request had been overridden. These tests pin
the replacement contract: a 400 in the OpenAI error shape, and no provider call.

The shape assertion is the point of the test, not decoration. An OpenAI client
reads `error.message`; if a later refactor moves this behind a pydantic
validator the status becomes 422 and the body becomes pydantic's error list,
and every one of those clients breaks silently. That is the regression this
file exists to catch.
"""

import pytest

from tests.test_auth import CHAT_BODY


@pytest.mark.asyncio
async def test_stream_true_is_rejected(client_scope_client):
    resp = await client_scope_client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    assert resp.status_code == 400

    error = resp.json()["error"]
    assert error["type"] == "unsupported_parameter"
    assert error["param"] == "stream"
    assert "not supported" in error["message"].lower()


@pytest.mark.asyncio
async def test_stream_true_never_reaches_the_router(client_scope_client, stub_router):
    """Rejecting after the provider call would still spend quota."""
    await client_scope_client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    stub_router.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_true_still_401s_when_unauthenticated(raw_client, stub_router):
    """Auth outranks validation: an anonymous caller must not be able to probe
    which parameters the gateway supports."""
    resp = await raw_client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    assert resp.status_code == 401
    stub_router.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_false_is_accepted(client_scope_client, stub_router):
    resp = await client_scope_client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": False}
    )
    assert resp.status_code != 400
    assert stub_router.await_count == 1


@pytest.mark.asyncio
async def test_absent_stream_is_accepted(client_scope_client, stub_router):
    """The default is False, so omitting the field must not trip the check."""
    resp = await client_scope_client.post("/v1/chat/completions", json=CHAT_BODY)
    assert resp.status_code != 400
    assert stub_router.await_count == 1
