"""
Tests for the Admin API endpoints.

Environment bootstrap and client construction live in tests/conftest.py — see
the ordering note there. These tests use the `admin_client` fixture, which
carries a valid ADMIN_TOKENS credential; the unauthenticated counterparts
live in tests/test_auth.py against the `raw_client` fixture.
"""

from pathlib import Path

import pytest

from gateway.main import app


@pytest.mark.asyncio
async def test_add_and_list_keys(admin_client):
    # Add a key
    resp = await admin_client.post(
        "/admin/keys",
        json={"provider": "groq", "api_key": "gsk_test123", "label": "test1"},
    )
    assert resp.status_code == 200
    key_id = resp.json()["id"]
    assert isinstance(key_id, int)

    # List keys
    resp = await admin_client.get("/admin/keys")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert any(k["id"] == key_id for k in keys)
    # Verify key is masked
    found = [k for k in keys if k["id"] == key_id][0]
    assert "gsk_test123" not in str(found)
    assert found["api_key_masked"].startswith("gsk_")


@pytest.mark.asyncio
async def test_delete_key(admin_client):
    resp = await admin_client.post(
        "/admin/keys",
        json={"provider": "groq", "api_key": "gsk_deleteme", "label": "del"},
    )
    key_id = resp.json()["id"]

    resp = await admin_client.delete(f"/admin/keys/{key_id}")
    assert resp.status_code == 200

    resp = await admin_client.delete(f"/admin/keys/{key_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_toggle_key(admin_client):
    resp = await admin_client.post(
        "/admin/keys",
        json={"provider": "groq", "api_key": "gsk_toggle", "label": "tog"},
    )
    key_id = resp.json()["id"]

    # Disable
    resp = await admin_client.patch(
        f"/admin/keys/{key_id}", json={"is_enabled": False}
    )
    assert resp.status_code == 200

    # Check it's disabled
    resp = await admin_client.get("/admin/keys")
    found = [k for k in resp.json()["keys"] if k["id"] == key_id][0]
    assert found["is_enabled"] == 0


@pytest.mark.asyncio
async def test_providers_endpoint(admin_client):
    await admin_client.post(
        "/admin/keys",
        json={"provider": "groq", "api_key": "gsk_prov1", "label": "p1"},
    )
    await admin_client.post(
        "/admin/keys",
        json={"provider": "groq", "api_key": "gsk_prov2", "label": "p2"},
    )

    resp = await admin_client.get("/admin/providers")
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    groq_prov = [p for p in providers if p["provider"] == "groq"]
    assert len(groq_prov) == 1
    assert groq_prov[0]["total_keys"] >= 2


@pytest.mark.asyncio
async def test_stats_endpoint(admin_client):
    await admin_client.post(
        "/admin/keys",
        json={"provider": "groq", "api_key": "gsk_stats", "label": "stats"},
    )

    resp = await admin_client.get("/admin/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_keys"] >= 1
    assert stats["active_keys"] >= 1
    assert isinstance(stats["keys"], list)


@pytest.mark.asyncio
async def test_keys_usage_endpoint(admin_client):
    """The seventh admin route. Previously untested, and the one most easily
    missed when enumerating what needs a guard."""
    resp = await admin_client.get("/admin/keys/usage")
    assert resp.status_code == 200
    assert isinstance(resp.json()["keys"], list)


@pytest.mark.asyncio
async def test_health(raw_client):
    resp = await raw_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_app_version_matches_the_version_file():
    """`app.version` is what /openapi.json reports, and it was a string literal.

    /health reads VERSION directly so it cannot drift, but the FastAPI
    constructor is not covered by the `docs consistency` CI job, which only
    checks README.md, the site, and CHANGELOG.md. Without this assert, the next
    release bump can silently leave the schema reporting the old number.
    """
    expected = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert app.version == expected
