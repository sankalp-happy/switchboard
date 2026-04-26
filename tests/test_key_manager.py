"""
Tests for KeyManager — CRUD, encryption round-trip, rate-limit parsing, key selection.
Uses an in-memory SQLite database (overrides SQLITE_DB_PATH).
"""

import os
import pytest
import pytest_asyncio
import tempfile

# Override DB path BEFORE importing anything that reads settings
_tmpdir = tempfile.mkdtemp()
os.environ["SQLITE_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["ENCRYPTION_KEY"] = "VGVzdEtleUZvclN3aXRjaGJvYXJkMTIzNDU2Nzg5MA=="  # test key

# Now we need a valid Fernet key — generate one deterministically
from cryptography.fernet import Fernet

_test_fernet_key = Fernet.generate_key().decode()
os.environ["ENCRYPTION_KEY"] = _test_fernet_key

from core.database import init_db
from core.key_manager import (
    KeyManager,
    encrypt_key,
    decrypt_key,
    mask_key,
    parse_rate_limit_headers,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh DB and clean between tests."""
    await init_db()
    from core.database import get_db
    db = await get_db()
    await db.execute("DELETE FROM api_keys")
    await db.execute("DELETE FROM provider_config")
    await db.commit()
    await db.close()
    yield


@pytest.fixture
def km():
    return KeyManager()


# ---- Encryption round-trip ----

def test_encrypt_decrypt_roundtrip():
    original = "gsk_abc123testkey456"
    encrypted = encrypt_key(original)
    assert encrypted != original
    decrypted = decrypt_key(encrypted)
    assert decrypted == original


def test_mask_key():
    assert mask_key("gsk_abcdefghijklmnop") == "gsk_...mnop"
    assert mask_key("short") == "****"


# ---- Rate-limit header parsing ----

def test_parse_rate_limit_headers():
    headers = {
        "x-ratelimit-remaining-tokens": "5000",
        "x-ratelimit-remaining-requests": "28",
        "x-ratelimit-reset-tokens": "1m30s",
        "x-ratelimit-reset-requests": "2s",
    }
    parsed = parse_rate_limit_headers(headers)
    assert parsed["rate_limit_remaining_tokens"] == 5000
    assert parsed["rate_limit_remaining_requests"] == 28
    assert parsed["rate_limit_reset_tokens"] == "1m30s"
    assert parsed["rate_limit_reset_requests"] == "2s"


def test_parse_rate_limit_headers_empty():
    assert parse_rate_limit_headers({}) == {}


# ---- CRUD ----

@pytest.mark.asyncio
async def test_add_and_list_keys(km):
    key_id = await km.add_key("groq", "gsk_test_key_12345", "test-label")
    assert isinstance(key_id, int)

    keys = await km.list_keys()
    assert len(keys) >= 1
    found = [k for k in keys if k["id"] == key_id]
    assert len(found) == 1
    assert found[0]["provider"] == "groq"
    assert found[0]["label"] == "test-label"
    assert "api_key_masked" in found[0]
    assert "api_key_encrypted" not in found[0]


@pytest.mark.asyncio
async def test_list_keys_by_provider(km):
    await km.add_key("groq", "gsk_key1", "k1")
    await km.add_key("openai", "sk_key2", "k2")

    groq_keys = await km.list_keys(provider="groq")
    assert all(k["provider"] == "groq" for k in groq_keys)

    openai_keys = await km.list_keys(provider="openai")
    assert all(k["provider"] == "openai" for k in openai_keys)


@pytest.mark.asyncio
async def test_delete_key(km):
    key_id = await km.add_key("groq", "gsk_delete_me", "del")
    assert await km.delete_key(key_id) is True
    assert await km.delete_key(key_id) is False  # already deleted


@pytest.mark.asyncio
async def test_toggle_key(km):
    key_id = await km.add_key("groq", "gsk_toggle_me", "tog")
    assert await km.toggle_key(key_id, False) is True

    keys = await km.list_keys()
    found = [k for k in keys if k["id"] == key_id]
    assert found[0]["is_enabled"] == 0

    assert await km.toggle_key(key_id, True) is True


# ---- Key selection ----

@pytest.mark.asyncio
async def test_get_available_key_basic(km):
    await km.add_key("groq", "gsk_avail_key_1", "avail1")
    api_key, key_id = await km.get_available_key("groq")
    assert api_key == "gsk_avail_key_1"
    assert isinstance(key_id, int)


@pytest.mark.asyncio
async def test_get_available_key_no_keys(km):
    with pytest.raises(RuntimeError, match="No enabled API keys"):
        await km.get_available_key("nonexistent")


@pytest.mark.asyncio
async def test_get_available_key_picks_highest_remaining(km):
    id1 = await km.add_key("groq", "gsk_low", "low")
    id2 = await km.add_key("groq", "gsk_high", "high")

    # Simulate rate limits
    from core.database import get_db

    db = await get_db()
    await db.execute(
        "UPDATE api_keys SET rate_limit_remaining_tokens = ? WHERE id = ?", (100, id1)
    )
    await db.execute(
        "UPDATE api_keys SET rate_limit_remaining_tokens = ? WHERE id = ?", (9000, id2)
    )
    await db.commit()
    await db.close()

    api_key, key_id = await km.get_available_key("groq")
    assert api_key == "gsk_high"
    assert key_id == id2


@pytest.mark.asyncio
async def test_mark_key_exhausted(km):
    key_id = await km.add_key("groq", "gsk_exhaust_me", "exhaust")
    await km.mark_key_exhausted(key_id)

    from core.database import get_db

    db = await get_db()
    cursor = await db.execute(
        "SELECT rate_limit_remaining_tokens FROM api_keys WHERE id = ?", (key_id,)
    )
    row = await cursor.fetchone()
    await db.close()
    assert row["rate_limit_remaining_tokens"] == 0


@pytest.mark.asyncio
async def test_update_rate_limits(km):
    key_id = await km.add_key("groq", "gsk_ratelimit", "rl")
    headers = {
        "x-ratelimit-remaining-tokens": "4500",
        "x-ratelimit-remaining-requests": "22",
        "x-ratelimit-reset-tokens": "30s",
    }
    await km.update_rate_limits(key_id, headers)

    from core.database import get_db

    db = await get_db()
    cursor = await db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    row = await cursor.fetchone()
    await db.close()
    assert row["rate_limit_remaining_tokens"] == 4500
    assert row["rate_limit_remaining_requests"] == 22
    assert row["last_used_at"] is not None


@pytest.mark.asyncio
async def test_openai_compatible_requires_base_url_and_model_cards(km):
    with pytest.raises(ValueError, match="base_url is required"):
        await km.add_key(
            "openai-compatible",
            "sk-openai-compatible",
            "oc-1",
            model_cards=["gpt-4o-mini"],
        )

    with pytest.raises(ValueError, match="model_cards is required"):
        await km.add_key(
            "openai-compatible",
            "sk-openai-compatible",
            "oc-1",
            base_url="https://example.com/v1",
        )


@pytest.mark.asyncio
async def test_add_and_list_openai_compatible_with_metadata(km):
    key_id = await km.add_key(
        "openai-compatible",
        "sk-openai-compatible",
        "oc-main",
        base_url="https://example.com/v1/",
        model_cards=["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o-mini"],
    )

    keys = await km.list_keys(provider="openai-compatible")
    found = next(k for k in keys if k["id"] == key_id)

    assert found["base_url"] == "https://example.com/v1"
    assert found["model_cards"] == ["gpt-4o-mini", "gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_get_available_key_for_model_prefers_explicit_match(km):
    wildcard_id = await km.add_key("groq", "gsk_wildcard", "groq-wildcard")
    explicit_id = await km.add_key(
        "openai-compatible",
        "sk-explicit",
        "oc-explicit",
        base_url="https://example.com/v1",
        model_cards=["gpt-4o-mini"],
    )

    api_key, key_id, provider, base_url, model_cards = await km.get_available_key_for_model(
        "gpt-4o-mini",
        supported_providers={"groq", "openai-compatible"},
    )

    assert api_key == "sk-explicit"
    assert key_id == explicit_id
    assert provider == "openai-compatible"
    assert base_url == "https://example.com/v1"
    assert model_cards == ["gpt-4o-mini"]
    assert key_id != wildcard_id


@pytest.mark.asyncio
async def test_get_available_key_for_model_uses_wildcard_when_no_explicit(km):
    key_id = await km.add_key("groq", "gsk_wildcard", "groq-wildcard")

    api_key, selected_id, provider, base_url, model_cards = await km.get_available_key_for_model(
        "llama-3.1-8b-instant",
        supported_providers={"groq", "openai-compatible"},
    )

    assert api_key == "gsk_wildcard"
    assert selected_id == key_id
    assert provider == "groq"
    assert base_url is None
    assert model_cards == []
