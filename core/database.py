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
