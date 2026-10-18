"""Phase C — read-only configuration endpoint (effective, validated config)."""

from __future__ import annotations

from fastapi import APIRouter

from lib.config import get_config

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def effective_config() -> dict:
    """The validated settings.yaml content (no secrets live in config)."""
    return get_config().model_dump()
