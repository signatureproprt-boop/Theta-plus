"""Phase G — read-only PAPER trading + ALERT_LOG API.

NO ORDER-PLACEMENT ENDPOINT EXISTS (§21/§28): every route here is a GET that
reports research state. There is no POST/PUT/DELETE, no broker call and no
execution adapter anywhere in this module.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from models.alert_models import AlertProviderStatus
from models.paper_models import PaperPosition, PaperSummary
from pipeline import get_pipeline

router = APIRouter(tags=["paper", "alerts"])


@router.get("/paper/positions", response_model=list[PaperPosition])
async def paper_positions() -> list[PaperPosition]:
    """All paper positions (open + closed). PAPER / HYPOTHETICAL only."""
    return get_pipeline().paper.positions


@router.get("/paper/positions/{paper_position_id}", response_model=PaperPosition)
async def paper_position(paper_position_id: str) -> PaperPosition:
    position = get_pipeline().paper.get(paper_position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="paper_position_id not found")
    return position


@router.get("/paper/trades", response_model=list[PaperPosition])
async def paper_trades() -> list[PaperPosition]:
    """PAPER_TRADE_LOG: closed paper trades only."""
    return get_pipeline().paper.trades()


@router.get("/paper/summary", response_model=PaperSummary)
async def paper_summary() -> PaperSummary:
    return get_pipeline().paper.summary()


@router.get("/alerts")
async def alerts() -> dict:
    """ALERT_LOG — credential-free records plus provider status."""
    pipeline = get_pipeline()
    return {
        "status": pipeline.alerts.status().model_dump(mode="json"),
        "records": [r.model_dump(mode="json") for r in pipeline.alerts.records()[-100:]],
    }


@router.get("/alerts/status", response_model=AlertProviderStatus)
async def alerts_status() -> AlertProviderStatus:
    return get_pipeline().alerts.status()
