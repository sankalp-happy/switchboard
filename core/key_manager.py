"""
API Key Manager — handles multi-key storage, Fernet encryption,
rate-limit tracking, and key selection for provider routing.
"""

import logging
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict, Any, Set
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from core.config import settings
from core.database import get_db

logger = logging.getLogger("switchboard.key_manager")


def _get_fernet() -> Fernet:
    """Return a Fernet instance using ENCRYPTION_KEY from settings."""
    key = settings.ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_key(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_key(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


def mask_key(plain: str) -> str:
    """Show first 4 and last 4 chars only."""
    if len(plain) <= 10:
        return "****"
    return f"{plain[:4]}...{plain[-4:]}"


# ---------------------------------------------------------------------------
# Rate-limit header parsing (Groq / OpenAI compatible)
# ---------------------------------------------------------------------------

def parse_rate_limit_headers(headers: dict) -> Dict[str, Any]:
    """
    Extract rate limit info from provider response headers.
    Groq headers:
        x-ratelimit-limit-requests, x-ratelimit-remaining-requests,
        x-ratelimit-reset-requests,
        x-ratelimit-limit-tokens, x-ratelimit-remaining-tokens,
        x-ratelimit-reset-tokens
    """
    mapping = {
        "rate_limit_remaining_tokens": "x-ratelimit-remaining-tokens",
        "rate_limit_remaining_requests": "x-ratelimit-remaining-requests",
        "rate_limit_reset_tokens": "x-ratelimit-reset-tokens",
        "rate_limit_reset_requests": "x-ratelimit-reset-requests",
    }
    result: Dict[str, Any] = {}
    for field, header in mapping.items():
        val = headers.get(header)
        if val is not None:
            if "remaining" in field:
                try:
                    result[field] = int(val)
                except ValueError:
                    pass
            else:
                result[field] = str(val)
    return result


def parse_duration_to_seconds(duration_str: str) -> float:
    """
    Parse Groq-style duration strings like '1m6s', '6.123s', '59m59s', '500ms'
    into total seconds (float).
    """
    if not duration_str:
        return 0.0
    total = 0.0
    # Match minutes
    m = re.search(r'(\d+)m(?!s)', duration_str)
    if m:
        total += int(m.group(1)) * 60
    # Match seconds (possibly fractional)
    s = re.search(r'([\d.]+)s$', duration_str)
    if s:
        total += float(s.group(1))
    # Match milliseconds
    ms = re.search(r'(\d+)ms', duration_str)
    if ms:
        total += int(ms.group(1)) / 1000.0
    return total if total > 0 else 60.0  # default 60s if we can't parse it


# ---------------------------------------------------------------------------
# CRUD + selection
# ---------------------------------------------------------------------------

class KeyManager:
    """Singleton-style manager — instantiate once at startup."""

    MIN_TOKENS_THRESHOLD = 100  # consider a key "exhausted" below this

    # ---- key CRUD ----

    async def add_key(
        self,
        provider: str,
        api_key: str,
        label: str = "",
        base_url: Optional[str] = None,
        model_cards: Optional[List[str]] = None,
    ) -> int:
        provider_normalized = provider.lower()
        normalized_base_url = self._normalize_base_url(base_url)
        normalized_model_cards = self._normalize_model_cards(model_cards)

        if provider_normalized == "openai-compatible":
            if not normalized_base_url:
                raise ValueError("base_url is required for provider 'openai-compatible'")

        encrypted = encrypt_key(api_key)
        db = await get_db()
        cursor = await db.execute(
            """
            INSERT INTO api_keys (provider, api_key_encrypted, label, base_url, model_cards)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                provider_normalized,
                encrypted,
                label,
                normalized_base_url,
                json.dumps(normalized_model_cards) if normalized_model_cards else None,
            ),
        )
        await db.commit()
        key_id = cursor.lastrowid
        logger.info(f"Added key id={key_id} provider={provider} label={label}")
        return key_id

    async def list_keys(self, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        db = await get_db()
        if provider:
            cursor = await db.execute(
                "SELECT * FROM api_keys WHERE provider = ? ORDER BY id",
                (provider.lower(),),
            )
        else:
            cursor = await db.execute("SELECT * FROM api_keys ORDER BY id")
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            try:
                plain = decrypt_key(d["api_key_encrypted"])
                d["api_key_masked"] = mask_key(plain)
            except Exception:
                d["api_key_masked"] = "****"
            raw_model_cards = d.get("model_cards")
            if raw_model_cards:
                try:
                    parsed_cards = json.loads(raw_model_cards)
                    d["model_cards"] = parsed_cards if isinstance(parsed_cards, list) else []
                except Exception:
                    d["model_cards"] = []
            else:
                d["model_cards"] = []
            del d["api_key_encrypted"]
            results.append(d)
        return results

    async def delete_key(self, key_id: int) -> bool:
        db = await get_db()
        cursor = await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        await db.commit()
        return cursor.rowcount > 0

    async def decrypt_key_by_id(self, key_id: int) -> str:
        db = await get_db()
        cursor = await db.execute("SELECT api_key_encrypted FROM api_keys WHERE id = ?", (key_id,))
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Key id={key_id} not found")
        return decrypt_key(row["api_key_encrypted"])

    async def toggle_key(self, key_id: int, enabled: bool) -> bool:
        db = await get_db()
        cursor = await db.execute(
            "UPDATE api_keys SET is_enabled = ? WHERE id = ?",
            (1 if enabled else 0, key_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    # ---- key selection ----

    async def get_available_key(self, provider: str) -> Tuple[str, int]:
        """
        Pick the best available key for a provider:
        1. Prefer keys that have NEVER been used (NULL remaining — treat as unlimited).
        2. Then keys with remaining_tokens > threshold, sorted DESC by remaining.
        3. Fallback: pick the key with the soonest reset time.
        4. If none at all, raise RuntimeError.
        Returns (decrypted_api_key, key_id).
        """
        db = await get_db()
        # First: keys with remaining tokens above threshold.
        # NULL means never used = unlimited, so sort those FIRST.
        cursor = await db.execute(
            """
            SELECT id, api_key_encrypted, rate_limit_remaining_tokens
            FROM api_keys
            WHERE provider = ? AND is_enabled = 1
              AND (rate_limit_remaining_tokens IS NULL
                   OR rate_limit_remaining_tokens > ?)
            ORDER BY
              CASE WHEN rate_limit_remaining_tokens IS NULL THEN 0 ELSE 1 END,
              rate_limit_remaining_tokens DESC
            """,
            (provider.lower(), self.MIN_TOKENS_THRESHOLD),
        )
        row = await cursor.fetchone()
        if row:
            plain = decrypt_key(row["api_key_encrypted"])
            return plain, row["id"]

        # Fallback: pick key with soonest reset
        cursor = await db.execute(
            """
            SELECT id, api_key_encrypted
            FROM api_keys
            WHERE provider = ? AND is_enabled = 1
            ORDER BY rate_limit_reset_tokens ASC NULLS LAST
            LIMIT 1
            """,
            (provider.lower(),),
        )
        row = await cursor.fetchone()
        if row:
            plain = decrypt_key(row["api_key_encrypted"])
            logger.warning(
                f"All keys for {provider} below threshold — using key id={row['id']} (soonest reset)"
            )
            return plain, row["id"]

        raise RuntimeError(f"No enabled API keys available for provider '{provider}'")

    async def get_available_key_for_model(
        self,
        model: str,
        exclude_key_ids: Optional[Set[int]] = None,
        supported_providers: Optional[Set[str]] = None,
    ) -> Tuple[str, int, str, Optional[str], List[str]]:
        """
        Pick the best key across providers for a requested model.
        Priority:
        1) Any key with matching model card and NULL remaining tokens (never used)
        2) Any key with matching model card and remaining tokens > threshold (highest remaining first)
        3) Any key with matching model card and earliest reset time
        """
        db = await get_db()
        cursor = await db.execute(
            """
            SELECT id, provider, api_key_encrypted, base_url, model_cards, rate_limit_remaining_tokens
            FROM api_keys
            WHERE is_enabled = 1
            ORDER BY
              CASE WHEN rate_limit_remaining_tokens IS NULL THEN 0 ELSE 1 END,
              rate_limit_remaining_tokens DESC,
              rate_limit_resets_at ASC
            """
        )
        rows = await cursor.fetchall()
        excluded = exclude_key_ids or set()
        supported = {p.lower() for p in supported_providers} if supported_providers else None

        normalized_model = (model or "").strip()
        if not normalized_model:
            raise RuntimeError("Model name is required")

        explicit_matches = []
        wildcard_matches = []
        for row in rows:
            if row["id"] in excluded:
                continue
            if supported and row["provider"] not in supported:
                continue
            cards = self._parse_model_cards(row["model_cards"])
            if cards:
                if normalized_model in cards:
                    explicit_matches.append(row)
            else:
                wildcard_matches.append(row)

        matching_rows = explicit_matches if explicit_matches else wildcard_matches

        if not matching_rows:
            raise RuntimeError(f"No enabled API keys support model '{normalized_model}'")

        for row in matching_rows:
            remaining = row["rate_limit_remaining_tokens"]
            if remaining is None or remaining > self.MIN_TOKENS_THRESHOLD:
                plain = decrypt_key(row["api_key_encrypted"])
                return plain, row["id"], row["provider"], row["base_url"], self._parse_model_cards(row["model_cards"])

        fallback = matching_rows[0]
        plain = decrypt_key(fallback["api_key_encrypted"])
        logger.warning(
            "All matching keys below threshold for model %s — using key id=%s",
            normalized_model,
            fallback["id"],
        )
        return plain, fallback["id"], fallback["provider"], fallback["base_url"], self._parse_model_cards(fallback["model_cards"])

    # ---- rate-limit updates ----

    async def update_rate_limits(self, key_id: int, headers: dict):
        """Parse rate-limit headers from a provider response and update the DB row."""
        parsed = parse_rate_limit_headers(headers)
        if not parsed:
            return
        sets = []
        vals = []
        for col, val in parsed.items():
            sets.append(f"{col} = ?")
            vals.append(val)
        # Compute absolute reset time from the reset duration headers
        reset_dur = parsed.get("rate_limit_reset_tokens") or parsed.get("rate_limit_reset_requests")
        if reset_dur:
            secs = parse_duration_to_seconds(str(reset_dur))
            resets_at = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()
            sets.append("rate_limit_resets_at = ?")
            vals.append(resets_at)
        sets.append("last_used_at = ?")
        vals.append(datetime.now(timezone.utc).isoformat())
        vals.append(key_id)
        sql = f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ?"
        db = await get_db()
        await db.execute(sql, vals)
        await db.commit()

    async def mark_key_exhausted(self, key_id: int):
        """Set remaining tokens/requests to 0 for a key."""
        now = datetime.now(timezone.utc)
        # Default reset in 60s if we don't have a better value
        default_resets_at = (now + timedelta(seconds=60)).isoformat()
        db = await get_db()
        await db.execute(
            """UPDATE api_keys
               SET rate_limit_remaining_tokens = 0,
                   rate_limit_remaining_requests = 0,
                   rate_limit_resets_at = COALESCE(rate_limit_resets_at, ?),
                   last_used_at = ?
               WHERE id = ?""",
            (default_resets_at, now.isoformat(), key_id),
        )
        await db.commit()
        logger.warning(f"Key id={key_id} marked as exhausted (resets_at={default_resets_at})")

    # ---- seeding helper ----

    async def seed_from_env(self):
        """If GROQ_API_KEY env var is set and DB is empty, auto-seed it."""
        if not settings.GROQ_API_KEY:
            return
        db = await get_db()
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM api_keys WHERE provider = 'groq'"
        )
        row = await cursor.fetchone()
        if row["cnt"] == 0:
            await self.add_key("groq", settings.GROQ_API_KEY, "env-default")
            logger.info("Seeded GROQ_API_KEY from environment into database.")

    @staticmethod
    def _normalize_base_url(base_url: Optional[str]) -> Optional[str]:
        if base_url is None:
            return None
        candidate = base_url.strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be a valid http/https URL")
        return candidate.rstrip("/")

    @staticmethod
    def _normalize_model_cards(model_cards: Optional[List[str]]) -> List[str]:
        if not model_cards:
            return []
        cleaned: List[str] = []
        seen = set()
        for item in model_cards:
            if not isinstance(item, str):
                continue
            model = item.strip()
            if not model:
                continue
            if model in seen:
                continue
            seen.add(model)
            cleaned.append(model)
        return cleaned

    @staticmethod
    def _parse_model_cards(raw_model_cards: Any) -> List[str]:
        if not raw_model_cards:
            return []
        if isinstance(raw_model_cards, list):
            return [x for x in raw_model_cards if isinstance(x, str) and x.strip()]
        if isinstance(raw_model_cards, str):
            try:
                parsed = json.loads(raw_model_cards)
                if isinstance(parsed, list):
                    return [x for x in parsed if isinstance(x, str) and x.strip()]
            except Exception:
                return []
        return []


    # ---- background sweeper ----

    async def reset_expired_keys(self) -> int:
        """
        Reset keys whose rate_limit_resets_at has passed.
        Sets remaining_tokens and remaining_requests back to NULL
        so they become eligible for selection again.
        Returns the number of keys reset.
        """
        now = datetime.now(timezone.utc).isoformat()
        db = await get_db()
        cursor = await db.execute(
            """UPDATE api_keys
               SET rate_limit_remaining_tokens = NULL,
                   rate_limit_remaining_requests = NULL,
                   rate_limit_resets_at = NULL
               WHERE rate_limit_resets_at IS NOT NULL
                 AND rate_limit_resets_at < ?
                 AND is_enabled = 1""",
            (now,),
        )
        await db.commit()
        if cursor.rowcount > 0:
            logger.info(f"Background sweeper: reset {cursor.rowcount} expired key(s)")
        return cursor.rowcount


# Module-level singleton
key_manager = KeyManager()
