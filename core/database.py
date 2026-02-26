"""
SQLite database layer for Switchboard configuration storage.
Stores API keys (encrypted) and provider configs.
"""

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


async def get_db() -> aiosqlite.Connection:
    """Get a new database connection."""
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    """Initialize the database schema. Called once at startup."""
    logger.info(f"Initializing SQLite database at {DB_PATH}")
    db = await get_db()
    try:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        logger.info("Database schema initialized successfully.")
    finally:
        await db.close()
