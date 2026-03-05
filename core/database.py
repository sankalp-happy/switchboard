"""
SQLite database layer for Switchboard configuration storage.
Stores API keys (encrypted) and provider configs.

Uses a singleton connection with WAL mode and busy_timeout
so concurrent async tasks don't get 'database is locked'.
"""

import asyncio
import aiosqlite
import os
import logging
from datetime import datetime, timedelta, timezone
from core.config import settings

logger = logging.getLogger("switchboard.database")

DB_PATH = settings.SQLITE_DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    rate_limit_remaining_tokens INTEGER,
    rate_limit_remaining_requests INTEGER,
    rate_limit_reset_tokens TEXT,
    rate_limit_reset_requests TEXT,
    rate_limit_resets_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provider_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL UNIQUE,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    base_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS key_usage_buckets (
    key_id INTEGER NOT NULL,
    bucket_minute TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, bucket_minute),
    FOREIGN KEY (key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_usage_bucket_minute
    ON key_usage_buckets(bucket_minute);
"""

# ---------------------------------------------------------------------------
# Singleton connection — avoids "database is locked" under concurrency
# ---------------------------------------------------------------------------
_db_conn: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    """Return the shared database connection (created on first call)."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    async with _db_lock:
        # Double-check after acquiring lock
        if _db_conn is not None:
            return _db_conn

        os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")  # wait up to 5s instead of failing
        await db.execute("PRAGMA foreign_keys=ON")
        _db_conn = db
        return _db_conn


async def init_db():
    """Initialize the database schema. Called once at startup."""
    logger.info(f"Initializing SQLite database at {DB_PATH}")
    db = await get_db()
    await db.executescript(SCHEMA_SQL)
    await db.commit()

    # --- lightweight migrations ---
    cursor = await db.execute("PRAGMA table_info(api_keys)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "rate_limit_resets_at" not in cols:
        await db.execute("ALTER TABLE api_keys ADD COLUMN rate_limit_resets_at TEXT")
        await db.commit()
        logger.info("Migrated: added rate_limit_resets_at column")

    logger.info("Database schema initialized successfully.")


# ---------------------------------------------------------------------------
# Per-key usage tracking (minute-level buckets)
# ---------------------------------------------------------------------------

async def record_usage(key_id: int, tokens: int = 0) -> None:
    """Increment request count and token total for the current minute bucket."""
    db = await get_db()
    bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    await db.execute(
        """
        INSERT INTO key_usage_buckets (key_id, bucket_minute, request_count, total_tokens)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(key_id, bucket_minute) DO UPDATE SET
            request_count = request_count + 1,
            total_tokens  = total_tokens + excluded.total_tokens
        """,
        (key_id, bucket, tokens),
    )
    await db.commit()


async def get_usage_stats(minutes: int) -> list[dict]:
    """
    Return per-key aggregated usage for the last `minutes` minutes.
    Returns list of dicts: {key_id, label, provider, request_count, total_tokens}
    """
    db = await get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")
    cursor = await db.execute(
        """
        SELECT k.id AS key_id, k.label, k.provider,
               COALESCE(SUM(u.request_count), 0) AS request_count,
               COALESCE(SUM(u.total_tokens), 0)  AS total_tokens
        FROM api_keys k
        LEFT JOIN key_usage_buckets u
            ON k.id = u.key_id AND u.bucket_minute >= ?
        GROUP BY k.id
        ORDER BY k.id
        """,
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "key_id": row["key_id"],
            "label": row["label"],
            "provider": row["provider"],
            "request_count": row["request_count"],
            "total_tokens": row["total_tokens"],
        }
        for row in rows
    ]


async def cleanup_old_buckets() -> int:
    """Delete usage buckets older than 25 hours. Returns rows deleted."""
    db = await get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M")
    cursor = await db.execute(
        "DELETE FROM key_usage_buckets WHERE bucket_minute < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount
