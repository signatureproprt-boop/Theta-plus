"""Phase E — read-only replay/backtest API. Offline; no execution surface."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from lib.config import get_config
from models.replay_models import ReplayResult
from replay.backtest_engine import run_replay
from replay.fixtures import synthetic_session
from replay.metrics import daily_report, metrics_csv, outcomes_csv, range_report, signals_csv
from storage.snapshot_store import SnapshotStore
from storage.sources import InMemorySource, SqliteSource

router = APIRouter(prefix="/replay", tags=["replay"])

_store: Optional[SnapshotStore] = None


def store() -> SnapshotStore:
    global _store
    if _store is None:
        _store = SnapshotStore()
    return _store


class ReplayRequest(BaseModel):
    source: str = "synthetic"  # synthetic | sqlite
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    session_date: str = "2025-01-15"  # synthetic fixture session
    strict_ordering: Optional[bool] = None


def _run(req: ReplayRequest) -> ReplayResult:
    config = get_config()
    if req.source == "sqlite":
        if store().count() == 0:
            raise HTTPException(
                status_code=409,
                detail="no historical snapshots stored: real-market validation is PENDING HISTORICAL DATA",
            )
        return run_replay(SqliteSource(store(), req.date_from, req.date_to), config,
                          data_origin="HISTORICAL", strict_ordering=req.strict_ordering)
    return run_replay(InMemorySource(synthetic_session(req.session_date)), config,
                      data_origin="SYNTHETIC", strict_ordering=req.strict_ordering)


@router.post("/run", response_model=ReplayResult)
async def replay_run(req: ReplayRequest) -> ReplayResult:
    return _run(req)


@router.post("/report/daily")
async def replay_daily_report(req: ReplayRequest) -> dict:
    result = _run(req)
    return daily_report(result.date_from, result.metrics)


@router.post("/report/range")
async def replay_range_report(req: ReplayRequest) -> dict:
    result = _run(req)
    return range_report(result.date_from, result.date_to, result.metrics)


@router.post("/export/{kind}")
async def replay_export(kind: str, req: ReplayRequest) -> Response:
    if kind not in ("signals", "outcomes", "metrics"):
        raise HTTPException(status_code=422, detail="kind must be signals, outcomes or metrics")
    result = _run(req)
    body = {
        "signals": lambda: signals_csv(result.signals),
        "outcomes": lambda: outcomes_csv(result.outcomes),
        "metrics": lambda: metrics_csv(result.metrics),
    }[kind]()
    return Response(
        content=body, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="replay_{kind}.csv"'},
    )


@router.get("/snapshots/status")
async def snapshots_status() -> dict:
    s = store()
    return {
        "stored_snapshots": s.count(),
        "sessions": s.sessions(),
        "db_path": str(s.path),
        "real_historical_data": "AVAILABLE" if s.count() else "PENDING",
    }
