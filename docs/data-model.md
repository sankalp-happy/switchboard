# Data model

Two stores. SQLite holds everything authoritative; Redis holds only the semantic cache and
can be lost without consequence beyond hit rate.

## SQLite

Path from `SQLITE_DB_PATH`, default `data/switchboard.db`, `/app/data/switchboard.db` under
Compose (a named volume, `switchboard-data`). Schema in `core/database.py`, `SCHEMA_SQL`.

### Connection handling

`get_db()` returns a **singleton** `aiosqlite.Connection`, created on first call under an
`asyncio.Lock` with a double-check inside the lock. Parent directories are created if
missing. Three pragmas are set on creation:

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers do not block the writer |
| `busy_timeout` | `5000` | Wait up to 5s for a lock instead of failing immediately |
| `foreign_keys` | `ON` | SQLite defaults this **off**; the usage-bucket cascade depends on it |

One shared connection rather than a pool is what avoids `database is locked` under
concurrent async tasks. It also means SQLite's single-writer nature is the real scaling
limit: fine for one gateway instance, not a horizontally-scalable configuration store.

`row_factory` is `aiosqlite.Row`, so query results are accessed by column name throughout.

### `api_keys`

One row per provider API key. This is the only table holding a secret.

| Column | Type | Default | Notes |
|---|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | | Referred to as `key_id` everywhere else, and used as the `key_label` metric label |
| `provider` | TEXT NOT NULL | | Lowercased on insert. Not constrained to the registry — an unroutable value is accepted |
| `api_key_encrypted` | TEXT NOT NULL | | Fernet token. Never leaves the process; deleted from every API response |
| `label` | TEXT NOT NULL | `''` | Human name. `env-default` marks a key seeded from `GROQ_API_KEY` |
| `is_enabled` | INTEGER NOT NULL | `1` | 0 excludes the key from every selection query. Set to 0 automatically on a 401 |
| `rate_limit_remaining_tokens` | INTEGER | `NULL` | `NULL` means "never used or reset" and is treated as **unlimited** |
| `rate_limit_remaining_requests` | INTEGER | `NULL` | Tracked but not used for selection |
| `rate_limit_reset_tokens` | TEXT | `NULL` | Raw vendor duration string, e.g. `"1m6s"` |
| `rate_limit_reset_requests` | TEXT | `NULL` | Same |
| `rate_limit_resets_at` | TEXT | `NULL` | Absolute UTC ISO timestamp, computed from the duration. This is what the sweeper reads |
| `last_used_at` | TEXT | `NULL` | UTC ISO, stamped on every rate-limit update |
| `created_at` | TEXT NOT NULL | `datetime('now')` | |

There is no unique constraint on `(provider, api_key_encrypted)` — and one could not work
anyway, since Fernet output is non-deterministic. The same key added twice produces two
independent rows with independent quota tracking.

There is no index on `provider` or `is_enabled`, which the selection queries filter on. At
realistic key counts (single digits) this is irrelevant.

### `key_usage_buckets`

Minute-resolution per-key traffic, powering `GET /admin/keys/usage`.

| Column | Type | Notes |
|---|---|---|
| `key_id` | INTEGER NOT NULL | FK to `api_keys(id)` **ON DELETE CASCADE** |
| `bucket_minute` | TEXT NOT NULL | UTC, format `%Y-%m-%dT%H:%M` |
| `request_count` | INTEGER NOT NULL DEFAULT 0 | |
| `total_tokens` | INTEGER NOT NULL DEFAULT 0 | prompt + completion |

Primary key `(key_id, bucket_minute)`; index `idx_usage_bucket_minute` on `bucket_minute`
for the range scans.

Because `bucket_minute` is a zero-padded ISO-like string, lexicographic comparison equals
chronological comparison — which is why the queries can use plain `>=` and `<` on text.

Deleting a key deletes its usage history, via the cascade — which only works because
`PRAGMA foreign_keys=ON` is set on the connection.

### `provider_config`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `provider` | TEXT NOT NULL UNIQUE | |
| `is_enabled` | INTEGER NOT NULL DEFAULT 1 | |
| `base_url` | TEXT NOT NULL DEFAULT `''` | |
| `created_at` | TEXT NOT NULL | |

**This table is currently unused.** Nothing writes to it and nothing reads it; provider base
URLs are hardcoded in the adapters and the enabled set is the hardcoded registry. It is
created by `init_db()` and truncated by the test fixture, and that is all. It represents an
intended future where providers are configurable at runtime.

## Migrations

There is no migration framework. `init_db()` runs `SCHEMA_SQL` (every statement is
`CREATE ... IF NOT EXISTS`, so it is idempotent) and then performs one hand-written check:

```python
cursor = await db.execute("PRAGMA table_info(api_keys)")
cols = {row[1] for row in await cursor.fetchall()}
if "rate_limit_resets_at" not in cols:
    await db.execute("ALTER TABLE api_keys ADD COLUMN rate_limit_resets_at TEXT")
```

This exists because `rate_limit_resets_at` was added after the initial release, and
`CREATE TABLE IF NOT EXISTS` will not alter an existing table. Any future column addition
needs the same treatment — a new column in `SCHEMA_SQL` alone will be invisible to every
database that already exists.

## Usage tracking functions

`core/database.py`.

### `record_usage(key_id, tokens)`

Upserts the current minute bucket:

```sql
INSERT INTO key_usage_buckets (key_id, bucket_minute, request_count, total_tokens)
VALUES (?, ?, 1, ?)
ON CONFLICT(key_id, bucket_minute) DO UPDATE SET
    request_count = request_count + 1,
    total_tokens  = total_tokens + excluded.total_tokens
```

Called by the router after a successful provider call, inside its own try/except so a
failure here cannot fail an otherwise-successful request.

### `get_usage_stats(minutes)`

`LEFT JOIN`s `api_keys` against buckets newer than the cutoff, grouped by key. The `LEFT`
join and the `COALESCE(SUM(...), 0)` are why keys with zero traffic still appear, with zeros,
rather than vanishing from the report.

### `cleanup_old_buckets()`

Deletes buckets older than **25 hours**, returning the row count. Runs every 600 seconds
from `_usage_bucket_cleanup`. The extra hour beyond the 24-hour reporting window is slack,
so the boundary of the `last_24h` query is never near the deletion boundary.

Storage is bounded at roughly `keys × 1500` rows.

## Redis

Semantic cache only. See [caching.md](caching.md) for the algorithm.

| | |
|---|---|
| Key | `nexus:cache:<sha256 of model_temperature_messagesJSON>` |
| Value | JSON: `{"embedding": [float...], "response": {...}, "original_model": "..."}` |
| TTL | 3600 seconds, set with `SETEX` |
| Access | `KEYS nexus:cache:*` then `GET` per key on every lookup |

The embedding is the largest part of each entry by far. Nothing in Redis is authoritative,
and there is no schema versioning — a change to the payload shape is handled by malformed
entries being skipped on read and expiring within the hour.
