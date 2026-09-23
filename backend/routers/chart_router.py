"""Phase F — chart signal API (read-only visualization surface).

GET /api/chart/signals returns backend-generated markers only. There is no
order, execution, buy/sell or webhook path here or anywhere else in V1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Query

from chart.markers import (
    link_invalidations,
    marker_from_decision,
    marker_from_replay_signal,
    origin_label,
    visible_markers,
)
from lib.config import get_config
from lib.dates import IST, to_ist
from models.chart_models import (
    ORIGIN_LIVE,
    ORIGIN_REPLAY_SYNTHETIC,
    ChartSignalMarker,
    ChartSignalsResponse,
)
from pipeline import get_pipeline
from replay.backtest_engine import run_replay
from replay.fixtures import synthetic_session
from storage.sources import InMemorySource

router = APIRouter(prefix="/chart", tags=["chart"])

LIVE_NOTE = (
    "LIVE pipeline behaviour on the SIMULATED feed — NOT real market data. "
    "Real Dhan market data: PENDING."
)
REPLAY_NOTE = (
    "SYNTHETIC replay fixture via the Phase E replay engine — correctness proof only, "
    "NOT market performance. Real historical data: PENDING."
)
AUTHORITY_NOTE = (
    "TradingView is the chart visualization layer only. VWAP/PCR/OI/score/decision/state "
    "shown here are computed by the backend engines and are authoritative; the widget's "
    "built-in VWAP is a visual reference."
)

# Deterministic synthetic replay markers, computed once (the replay engine is pure).
_replay_cache: Optional[list[ChartSignalMarker]] = None


def replay_markers() -> list[ChartSignalMarker]:
    global _replay_cache
    if _replay_cache is None:
        result = run_replay(InMemorySource(synthetic_session()), get_config(), data_origin="SYNTHETIC")
        _replay_cache = link_invalidations(
            [marker_from_replay_signal(s) for s in result.signals]
        )
    return _replay_cache


@router.get("/signals", response_model=ChartSignalsResponse)
async def chart_signals(
    origin: Literal["LIVE", "REPLAY-SYNTHETIC"] = Query(default="LIVE"),
    include_wait: bool = Query(default=False, description="WAIT markers are hidden by default"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> ChartSignalsResponse:
    config = get_config()
    now = datetime.now(IST)

    if origin == ORIGIN_REPLAY_SYNTHETIC:
        all_markers = replay_markers()
        notes = (REPLAY_NOTE, AUTHORITY_NOTE)
        latest = all_markers[-1] if all_markers else None
        spot = latest.spot if latest else None
        vwap = latest.vwap if latest else None
        distance = latest.vwap_distance if latest else None
        health = latest.data_health if latest else "OK"
        latest_decision = latest.decision if latest else "WAIT"
        latest_state = latest.state if latest else "WAIT"
    else:
        pipeline = get_pipeline()
        all_markers = link_invalidations(list(pipeline.chart_markers))
        notes = (LIVE_NOTE, AUTHORITY_NOTE)
        decision, features = pipeline.decision, pipeline.features
        spot = features.spot if features else None
        vwap = features.vwap.vwap if features else None
        distance = features.vwap.distance if features else None
        health = decision.data_health if decision else "OK"
        latest_decision = decision.decision if decision else "WAIT"
        latest_state = decision.state if decision else "WAIT"

    shown = visible_markers(all_markers, include_wait=include_wait)[-limit:]
    return ChartSignalsResponse(
        symbol=config.instrument,
        timeframe="5",
        timezone=config.timezone,
        data_origin=origin,
        data_origin_label=origin_label(origin),
        include_wait=include_wait,
        generated_at=now,
        count=len(shown),
        markers=tuple(shown),
        latest_decision=latest_decision,
        latest_state=latest_state,
        spot=spot,
        vwap=vwap,
        vwap_distance=distance,
        data_health=health or "OK",
        stale="STALE" in (health or "").upper(),
        notes=notes,
    )


@router.get("/signals/{signal_id}", response_model=ChartSignalMarker)
async def chart_signal_detail(signal_id: str) -> ChartSignalMarker:
    """One marker by id (live buffer first, then the synthetic replay set)."""
    from fastapi import HTTPException

    pool = link_invalidations(list(get_pipeline().chart_markers)) + replay_markers()
    for marker in pool:
        if marker.signal_id == signal_id:
            return marker
    raise HTTPException(status_code=404, detail="signal_id not found")


# Kept for parity checks: a marker built from the CURRENT live decision, so the
# frontend can never be the source of a signal.
@router.get("/latest", response_model=Optional[ChartSignalMarker])
async def chart_latest() -> Optional[ChartSignalMarker]:
    pipeline = get_pipeline()
    if pipeline.decision is None:
        return None
    return marker_from_decision(
        pipeline.decision, pipeline.features, data_origin=ORIGIN_LIVE
    ).model_copy(update={"timestamp": to_ist(pipeline.decision.timestamp)})
