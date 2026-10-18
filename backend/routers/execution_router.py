"""Phase H — read-only execution status/log API.

There is deliberately NO endpoint that enables, arms, or submits anything:
with no server-side authentication/role system present, live execution control
is DISABLED (§46). Arming is an explicit operator action on the server
(environment switches), never a UI click.
"""

from __future__ import annotations

from fastapi import APIRouter

from models.execution_models import ExecutionStatusPanel, ReconciliationReport
from pipeline import get_pipeline

router = APIRouter(prefix="/execution", tags=["execution"])


@router.get("/status", response_model=ExecutionStatusPanel)
async def execution_status() -> ExecutionStatusPanel:
    return get_pipeline().execution.status()


@router.get("/log")
async def execution_log() -> dict:
    """EXECUTION_LOG (credential-free)."""
    service = get_pipeline().execution
    return {"entries": [e.model_dump(mode="json") for e in service.entries()[-100:]]}


@router.get("/submissions")
async def execution_orders() -> dict:
    service = get_pipeline().execution
    return {"orders": [o.model_dump(mode="json") for o in service.orders.values()]}


@router.get("/reconciliation", response_model=ReconciliationReport)
async def execution_reconciliation() -> ReconciliationReport:
    return get_pipeline().execution.reconciler.report
