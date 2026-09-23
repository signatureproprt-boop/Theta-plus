"""Phase I — research/validation API (read-only; offline datasets only).

No endpoint can change a strategy parameter, arm execution or fabricate data.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from lib.config import get_config
from models.research_models import (
    ORIGIN_REAL_HISTORICAL,
    DatasetStatus,
    ReadinessReport,
    RealValidationReport,
)
from execution.instruments import instrument_map_status
from models.research_models import DeterminismAudit, LeakageAudit
from research.audits import audit_determinism_runs, audit_mutation_leakage
from research.ingest import RealDataError, load_real_source
from research.oos import OosReport, run_oos
from research.parity import ParityReport, live_replay_parity
from research.validation import (
    dataset_status,
    evidence_csv,
    readiness_report,
    real_dataset_path,
    validate_dataset,
)

router = APIRouter(prefix="/research", tags=["research"])

_cache: dict[str, RealValidationReport] = {}


class ValidateRequest(BaseModel):
    path: Optional[str] = None  # defaults to REAL_HISTORICAL_PATH
    data_origin: str = ORIGIN_REAL_HISTORICAL
    expected_interval_seconds: Optional[int] = None


class DeterminismRuns(BaseModel):
    audit: DeterminismAudit
    run_hashes: list[str] = []
    identical: bool = False


def _snapshots(req: ValidateRequest):
    """Load snapshots for an audit endpoint. Never fabricates a dataset."""
    target = req.path or (str(real_dataset_path()) if real_dataset_path() else None)
    if not target:
        raise HTTPException(
            status_code=409,
            detail=("REAL DATA = PENDING: no dataset configured (set REAL_HISTORICAL_PATH "
                    "or pass an explicit path). No data was fabricated."),
        )
    try:
        return list(load_real_source(target).iter_snapshots())
    except RealDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _run(req: ValidateRequest) -> RealValidationReport:
    target = req.path or (str(real_dataset_path()) if real_dataset_path() else None)
    if not target:
        raise HTTPException(
            status_code=409,
            detail=("REAL HISTORICAL VALIDATION = PENDING: no dataset configured "
                    "(set REAL_HISTORICAL_PATH or pass an explicit path). "
                    "No data was fabricated."),
        )
    try:
        report = validate_dataset(
            target, get_config(), data_origin=req.data_origin,
            expected_interval_seconds=req.expected_interval_seconds,
        )
    except RealDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    _cache["last"] = report
    return report


@router.get("/status", response_model=DatasetStatus)
async def research_status() -> DatasetStatus:
    return dataset_status()


@router.get("/readiness", response_model=ReadinessReport)
async def research_readiness() -> ReadinessReport:
    return readiness_report()


@router.post("/validate", response_model=RealValidationReport)
async def research_validate(req: ValidateRequest) -> RealValidationReport:
    return _run(req)


@router.get("/report", response_model=RealValidationReport)
async def research_last_report() -> RealValidationReport:
    report = _cache.get("last")
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="no validation has been run in this process yet",
        )
    return report


@router.get("/instrument-map")
async def research_instrument_map() -> dict:
    """Structural status of the operator-supplied Dhan instrument mapping."""
    return instrument_map_status()


@router.post("/parity", response_model=ParityReport)
async def research_parity(req: ValidateRequest) -> ParityReport:
    """LIVE pipeline vs Phase E replay on identical snapshots (§13)."""
    snapshots = _snapshots(req)
    return live_replay_parity(snapshots, get_config())


@router.post("/leakage/mutation", response_model=LeakageAudit)
async def research_mutation_leakage(req: ValidateRequest) -> LeakageAudit:
    """Distort every input AFTER T; decisions at/before T must not change (§7)."""
    return audit_mutation_leakage(_snapshots(req), get_config())


@router.post("/determinism", response_model=DeterminismRuns)
async def research_determinism(req: ValidateRequest) -> DeterminismRuns:
    """Three identical runs must produce one hash (§8)."""
    audit, hashes = audit_determinism_runs(_snapshots(req), get_config(), req.data_origin)
    return DeterminismRuns(audit=audit, run_hashes=list(hashes),
                           identical=len(set(hashes)) == 1)


@router.post("/oos", response_model=OosReport)
async def research_oos(req: ValidateRequest) -> OosReport:
    """Chronological 60/20/20 split; PENDING when history is insufficient (§10)."""
    return run_oos(_snapshots(req), get_config(), req.data_origin)


@router.post("/export/evidence")
async def research_export_evidence(req: ValidateRequest) -> Response:
    report = _run(req)
    return Response(
        content=evidence_csv(report),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{report.dataset.dataset_id}.csv"'},
    )
