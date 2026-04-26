"""
Admin API — CRUD for API keys and provider management.
Mounted at /admin in the gateway.
"""

import json
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.key_manager import key_manager
from core.database import get_db, get_usage_stats
from providers.groq_provider import GroqProvider
from providers.openai_compatible_provider import OpenAICompatibleProvider

logger = logging.getLogger("switchboard.admin")

admin_router = APIRouter(prefix="/admin", tags=["admin"])


# ---- request/response models ----

class AddKeyRequest(BaseModel):
    provider: str
    api_key: str
    label: str = ""
    base_url: Optional[str] = None
    model_cards: Optional[List[str]] = None


class ToggleKeyRequest(BaseModel):
    is_enabled: bool


# ---- endpoints ----

@admin_router.post("/keys")
async def add_key(body: AddKeyRequest):
    """Add a new API key for a provider."""
    try:
        key_id = await key_manager.add_key(
            provider=body.provider,
            api_key=body.api_key,
            label=body.label,
            base_url=body.base_url,
            model_cards=body.model_cards,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": key_id, "message": "Key added successfully"}


@admin_router.get("/keys")
async def list_keys(provider: Optional[str] = None):
    """List all API keys (masked). Optionally filter by provider."""
    keys = await key_manager.list_keys(provider=provider)
    return {"keys": keys}


@admin_router.delete("/keys/{key_id}")
async def delete_key(key_id: int):
    """Delete an API key by ID."""
    deleted = await key_manager.delete_key(key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"message": "Key deleted"}


@admin_router.patch("/keys/{key_id}")
async def toggle_key(key_id: int, body: ToggleKeyRequest):
    """Enable or disable an API key."""
    updated = await key_manager.toggle_key(key_id, body.is_enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"message": f"Key {'enabled' if body.is_enabled else 'disabled'}"}


@admin_router.get("/providers")
async def list_providers():
    """List providers with key counts and health summary."""
    all_keys = await key_manager.list_keys()
    providers: dict = {}
    for k in all_keys:
        prov = k["provider"]
        if prov not in providers:
            providers[prov] = {
                "provider": prov,
                "total_keys": 0,
                "enabled_keys": 0,
                "keys_with_quota": 0,
            }
        providers[prov]["total_keys"] += 1
        if k["is_enabled"]:
            providers[prov]["enabled_keys"] += 1
        remaining = k.get("rate_limit_remaining_tokens")
        if remaining is None or remaining > 100:
            providers[prov]["keys_with_quota"] += 1
    return {"providers": list(providers.values())}


@admin_router.get("/keys/usage")
async def keys_usage():
    """Per-key request counts and token totals for last 24h and last 1 minute."""
    stats_24h = await get_usage_stats(minutes=1440)
    stats_1m = await get_usage_stats(minutes=1)

    # Index 1-minute stats by key_id for easy merge
    one_min_map = {s["key_id"]: s for s in stats_1m}

    result = []
    for s in stats_24h:
        kid = s["key_id"]
        one_min = one_min_map.get(kid, {})
        result.append({
            "id": kid,
            "label": s["label"],
            "provider": s["provider"],
            "last_24h": {
                "request_count": s["request_count"],
                "total_tokens": s["total_tokens"],
            },
            "last_1m": {
                "request_count": one_min.get("request_count", 0),
                "total_tokens": one_min.get("total_tokens", 0),
            },
        })
    return {"keys": result}


@admin_router.get("/stats")
async def get_stats():
    """Basic stats: total keys, active keys, rate-limit status per key."""
    all_keys = await key_manager.list_keys()
    total = len(all_keys)
    active = sum(1 for k in all_keys if k["is_enabled"])
    return {
        "total_keys": total,
        "active_keys": active,
        "keys": [
            {
                "id": k["id"],
                "provider": k["provider"],
                "label": k["label"],
                "is_enabled": bool(k["is_enabled"]),
                "api_key_masked": k["api_key_masked"],
                "rate_limit_remaining_tokens": k.get("rate_limit_remaining_tokens"),
                "rate_limit_remaining_requests": k.get("rate_limit_remaining_requests"),
                "rate_limit_reset_tokens": k.get("rate_limit_reset_tokens"),
                "rate_limit_reset_requests": k.get("rate_limit_reset_requests"),
                "last_used_at": k.get("last_used_at"),
            }
            for k in all_keys
        ],
    }


@admin_router.post("/discover-models/{key_id}")
async def discover_models(key_id: int):
    """Fetch available models from a provider's API and update that key's model_cards."""
    keys = await key_manager.list_keys()
    key = next((k for k in keys if k["id"] == key_id), None)
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")

    provider_name = key["provider"]
    try:
        api_key_plain = await key_manager.decrypt_key_by_id(key_id)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt API key")

    if provider_name == "groq":
        provider = GroqProvider(api_key=api_key_plain)
    elif provider_name == "openai-compatible":
        base_url = key.get("base_url")
        if not base_url:
            raise HTTPException(status_code=400, detail="Key has no base_url configured")
        provider = OpenAICompatibleProvider(api_key=api_key_plain, base_url=base_url, provider_name=provider_name)
    else:
        raise HTTPException(status_code=400, detail=f"Model discovery not supported for provider '{provider_name}'")

    models = await provider.list_models()
    if not models:
        raise HTTPException(status_code=502, detail="No models returned from provider API")

    model_ids = [m.get("id", m.get("name", "")) for m in models if m.get("id") or m.get("name")]
    model_ids = [m for m in model_ids if m]

    db = await get_db()
    await db.execute(
        "UPDATE api_keys SET model_cards = ? WHERE id = ?",
        (json.dumps(model_ids), key_id),
    )
    await db.commit()

    return {"key_id": key_id, "discovered_models": model_ids, "count": len(model_ids)}
