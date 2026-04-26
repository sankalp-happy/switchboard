"""
Tests for the Admin API endpoints.
Uses FastAPI TestClient with an in-memory SQLite DB.
"""

import os
import tempfile

# Override settings before importing app
_tmpdir = tempfile.mkdtemp()
os.environ["SQLITE_DB_PATH"] = os.path.join(_tmpdir, "test_admin.db")

from cryptography.fernet import Fernet

os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["GROQ_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["REDIS_URL"] = "redis://localhost:6379/0"

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from core.database import init_db, get_db
from gateway.main import app


@pytest_asyncio.fixture(autouse=True)
async def setup():
    await init_db()
    # Clean between tests
    db = await get_db()
    await db.execute("DELETE FROM api_keys")
    await db.execute("DELETE FROM provider_config")
    await db.commit()
    await db.close()
    yield


@pytest.mark.asyncio
async def test_add_and_list_keys():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Add a key
        resp = await client.post(
            "/admin/keys",
            json={"provider": "groq", "api_key": "gsk_test123", "label": "test1"},
        )
        assert resp.status_code == 200
        key_id = resp.json()["id"]
        assert isinstance(key_id, int)

        # List keys
        resp = await client.get("/admin/keys")
        assert resp.status_code == 200
        keys = resp.json()["keys"]
        assert any(k["id"] == key_id for k in keys)
        # Verify key is masked
        found = [k for k in keys if k["id"] == key_id][0]
        assert "gsk_test123" not in str(found)
        assert found["api_key_masked"].startswith("gsk_")
        assert found["base_url"] is None
        assert found["model_cards"] == []


@pytest.mark.asyncio
async def test_add_openai_compatible_key():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/keys",
            json={
                "provider": "openai-compatible",
                "api_key": "sk_test_openai_compatible",
                "label": "oc-1",
                "base_url": "https://example.com/v1/",
                "model_cards": ["gpt-4o-mini", "gpt-4.1-mini"],
            },
        )
        assert resp.status_code == 200

        resp = await client.get("/admin/keys?provider=openai-compatible")
        assert resp.status_code == 200
        keys = resp.json()["keys"]
        assert len(keys) == 1
        assert keys[0]["base_url"] == "https://example.com/v1"
        assert keys[0]["model_cards"] == ["gpt-4o-mini", "gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_add_openai_compatible_requires_base_url_and_model_cards():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/keys",
            json={
                "provider": "openai-compatible",
                "api_key": "sk_test_openai_compatible",
                "label": "oc-1",
                "model_cards": ["gpt-4o-mini"],
            },
        )
        assert resp.status_code == 400
        assert "base_url is required" in resp.text

        resp = await client.post(
            "/admin/keys",
            json={
                "provider": "openai-compatible",
                "api_key": "sk_test_openai_compatible",
                "label": "oc-1",
                "base_url": "https://example.com/v1",
            },
        )
        assert resp.status_code == 400
        assert "model_cards is required" in resp.text


@pytest.mark.asyncio
async def test_delete_key():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/keys",
            json={"provider": "groq", "api_key": "gsk_deleteme", "label": "del"},
        )
        key_id = resp.json()["id"]

        resp = await client.delete(f"/admin/keys/{key_id}")
        assert resp.status_code == 200

        resp = await client.delete(f"/admin/keys/{key_id}")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_toggle_key():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/admin/keys",
            json={"provider": "groq", "api_key": "gsk_toggle", "label": "tog"},
        )
        key_id = resp.json()["id"]

        # Disable
        resp = await client.patch(
            f"/admin/keys/{key_id}", json={"is_enabled": False}
        )
        assert resp.status_code == 200

        # Check it's disabled
        resp = await client.get("/admin/keys")
        found = [k for k in resp.json()["keys"] if k["id"] == key_id][0]
        assert found["is_enabled"] == 0


@pytest.mark.asyncio
async def test_providers_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/keys",
            json={"provider": "groq", "api_key": "gsk_prov1", "label": "p1"},
        )
        await client.post(
            "/admin/keys",
            json={"provider": "groq", "api_key": "gsk_prov2", "label": "p2"},
        )

        resp = await client.get("/admin/providers")
        assert resp.status_code == 200
        providers = resp.json()["providers"]
        groq_prov = [p for p in providers if p["provider"] == "groq"]
        assert len(groq_prov) == 1
        assert groq_prov[0]["total_keys"] >= 2


@pytest.mark.asyncio
async def test_stats_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/keys",
            json={"provider": "groq", "api_key": "gsk_stats", "label": "stats"},
        )

        resp = await client.get("/admin/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total_keys"] >= 1
        assert stats["active_keys"] >= 1
        assert isinstance(stats["keys"], list)


@pytest.mark.asyncio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
