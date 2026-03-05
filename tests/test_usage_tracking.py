"""
Tests for per-key usage tracking — record_usage, get_usage_stats, cleanup_old_buckets.
Uses an in-memory SQLite database (overrides SQLITE_DB_PATH).
"""

import os
import pytest
import pytest_asyncio
import tempfile
from datetime import datetime, timedelta, timezone

# Override DB path BEFORE importing anything that reads settings
_tmpdir = tempfile.mkdtemp()
os.environ["SQLITE_DB_PATH"] = os.path.join(_tmpdir, "test_usage.db")

from cryptography.fernet import Fernet

os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from core.database import (
    init_db,
    get_db,
    record_usage,
    get_usage_stats,
    cleanup_old_buckets,
)
from core.key_manager import KeyManager


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh DB and clean between tests."""
    await init_db()
    db = await get_db()
    await db.execute("DELETE FROM key_usage_buckets")
    await db.execute("DELETE FROM api_keys")
    await db.commit()
    yield


@pytest.fixture
def km():
    return KeyManager()


async def _add_test_key(km: KeyManager, label: str = "test-key") -> int:
    """Helper to add a test key and return its ID."""
    return await km.add_key(provider="groq", api_key="sk-test-123", label=label)


# ---- record_usage ----


@pytest.mark.asyncio
async def test_record_usage_creates_bucket(km):
    key_id = await _add_test_key(km)
    await record_usage(key_id, tokens=100)

    db = await get_db()
    cursor = await db.execute(
        "SELECT request_count, total_tokens FROM key_usage_buckets WHERE key_id = ?",
        (key_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["request_count"] == 1
    assert row["total_tokens"] == 100


@pytest.mark.asyncio
async def test_record_usage_increments_existing_bucket(km):
    key_id = await _add_test_key(km)
    await record_usage(key_id, tokens=50)
    await record_usage(key_id, tokens=30)

    db = await get_db()
    cursor = await db.execute(
        "SELECT request_count, total_tokens FROM key_usage_buckets WHERE key_id = ?",
        (key_id,),
    )
    row = await cursor.fetchone()
    assert row["request_count"] == 2
    assert row["total_tokens"] == 80


@pytest.mark.asyncio
async def test_record_usage_separate_keys(km):
    kid1 = await _add_test_key(km, label="key-1")
    kid2 = await _add_test_key(km, label="key-2")

    await record_usage(kid1, tokens=100)
    await record_usage(kid2, tokens=200)
    await record_usage(kid1, tokens=50)

    db = await get_db()
    cursor = await db.execute(
        "SELECT request_count, total_tokens FROM key_usage_buckets WHERE key_id = ?",
        (kid1,),
    )
    row = await cursor.fetchone()
    assert row["request_count"] == 2
    assert row["total_tokens"] == 150

    cursor = await db.execute(
        "SELECT request_count, total_tokens FROM key_usage_buckets WHERE key_id = ?",
        (kid2,),
    )
    row = await cursor.fetchone()
    assert row["request_count"] == 1
    assert row["total_tokens"] == 200


# ---- get_usage_stats ----


@pytest.mark.asyncio
async def test_get_usage_stats_returns_all_keys(km):
    kid1 = await _add_test_key(km, label="key-a")
    kid2 = await _add_test_key(km, label="key-b")

    await record_usage(kid1, tokens=100)

    stats = await get_usage_stats(minutes=1440)
    assert len(stats) == 2

    s1 = next(s for s in stats if s["key_id"] == kid1)
    s2 = next(s for s in stats if s["key_id"] == kid2)

    assert s1["request_count"] == 1
    assert s1["total_tokens"] == 100
    assert s1["label"] == "key-a"
    assert s1["provider"] == "groq"

    # Key with no usage should show zeros
    assert s2["request_count"] == 0
    assert s2["total_tokens"] == 0


@pytest.mark.asyncio
async def test_get_usage_stats_respects_time_window(km):
    """Buckets outside the time window should not be counted."""
    key_id = await _add_test_key(km)

    db = await get_db()
    now = datetime.now(timezone.utc)

    # Insert a bucket from 2 hours ago (should be in 24h but not in 1m)
    old_bucket = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    await db.execute(
        "INSERT INTO key_usage_buckets (key_id, bucket_minute, request_count, total_tokens) VALUES (?, ?, 5, 500)",
        (key_id, old_bucket),
    )
    await db.commit()

    # Also record current usage
    await record_usage(key_id, tokens=100)

    stats_24h = await get_usage_stats(minutes=1440)
    s24 = next(s for s in stats_24h if s["key_id"] == key_id)
    assert s24["request_count"] == 6  # 5 old + 1 current
    assert s24["total_tokens"] == 600  # 500 old + 100 current

    stats_1m = await get_usage_stats(minutes=1)
    s1 = next(s for s in stats_1m if s["key_id"] == key_id)
    assert s1["request_count"] == 1  # only current
    assert s1["total_tokens"] == 100


# ---- cleanup_old_buckets ----


@pytest.mark.asyncio
async def test_cleanup_old_buckets(km):
    key_id = await _add_test_key(km)

    db = await get_db()
    now = datetime.now(timezone.utc)

    # Insert an old bucket (26 hours ago — older than 25h cutoff)
    old_bucket = (now - timedelta(hours=26)).strftime("%Y-%m-%dT%H:%M")
    await db.execute(
        "INSERT INTO key_usage_buckets (key_id, bucket_minute, request_count, total_tokens) VALUES (?, ?, 10, 1000)",
        (key_id, old_bucket),
    )
    # Insert a recent bucket (1 hour ago — should be kept)
    recent_bucket = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    await db.execute(
        "INSERT INTO key_usage_buckets (key_id, bucket_minute, request_count, total_tokens) VALUES (?, ?, 3, 300)",
        (key_id, recent_bucket),
    )
    await db.commit()

    deleted = await cleanup_old_buckets()
    assert deleted == 1

    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM key_usage_buckets WHERE key_id = ?",
        (key_id,),
    )
    row = await cursor.fetchone()
    assert row["cnt"] == 1  # only the recent bucket remains


@pytest.mark.asyncio
async def test_cleanup_does_not_delete_recent_buckets(km):
    key_id = await _add_test_key(km)
    await record_usage(key_id, tokens=50)

    deleted = await cleanup_old_buckets()
    assert deleted == 0

    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM key_usage_buckets WHERE key_id = ?",
        (key_id,),
    )
    row = await cursor.fetchone()
    assert row["cnt"] == 1


# ---- cascade delete ----


@pytest.mark.asyncio
async def test_deleting_key_cascades_usage(km):
    """When an API key is deleted, its usage buckets should be removed too."""
    key_id = await _add_test_key(km)
    await record_usage(key_id, tokens=100)

    db = await get_db()
    # Verify usage exists
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM key_usage_buckets WHERE key_id = ?",
        (key_id,),
    )
    assert (await cursor.fetchone())["cnt"] == 1

    # Delete the key
    await km.delete_key(key_id)

    # Usage should be gone
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM key_usage_buckets WHERE key_id = ?",
        (key_id,),
    )
    assert (await cursor.fetchone())["cnt"] == 0
