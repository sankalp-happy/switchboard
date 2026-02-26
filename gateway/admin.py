"""
Admin API — CRUD for API keys and provider management.
Mounted at /admin in the gateway.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.key_manager import key_manager

logger = logging.getLogger("switchboard.admin")

admin_router = APIRouter(prefix="/admin", tags=["admin"])


# ---- request/response models ----

class AddKeyRequest(BaseModel):
    provider: str
    api_key: str
    label: str = ""


class ToggleKeyRequest(BaseModel):
    is_enabled: bool


# ---- endpoints ----

@admin_router.post("/keys")
async def add_key(body: AddKeyRequest):
    """Add a new API key for a provider."""
    key_id = await key_manager.add_key(
        provider=body.provider,
        api_key=body.api_key,
        label=body.label,
    )
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
