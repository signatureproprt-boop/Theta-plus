"""Phase D — control-room API: dashboard payload, sheets status, system log,
and a manual tick hook. All read-only; no execution surface exists."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from lib.dates import IST
from models.dashboard_models import DashboardPayload
from pipeline import get_pipeline

router = APIRouter(tags=["dashboard"])


class TickRequest(BaseModel):
    at_time: Optional[datetime] = None  # naive => IST


@router.get("/dashboard", response_model=DashboardPayload)
async def dashboard() -> DashboardPayload:
    """The control-room payload (mirrors the DASHBOARD sheet exactly)."""
    return get_pipeline().dashboard()


@router.get("/system-log")
async def system_log() -> dict:
    return {"entries": [e.model_dump(mode="json") for e in get_pipeline().system_log.entries()[-50:]]}


@router.get("/sheets/status")
async def sheets_status() -> dict:
    return get_pipeline().sheets_status().model_dump(mode="json")


@router.post("/pipeline/tick", response_model=DashboardPayload)
async def pipeline_tick(req: TickRequest) -> DashboardPayload:
    """Force one deterministic pipeline iteration (dev/research harness)."""
    now = req.at_time or datetime.now(IST)
    return await get_pipeline().tick(now)
