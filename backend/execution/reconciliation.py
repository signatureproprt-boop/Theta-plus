"""Phase H — PositionReconciliationService (§37/§38/§39).

The BROKER is authoritative for live positions. Until a reconcile completes
successfully the state is PENDING and live execution is blocked; an unexpected
broker position produces MISMATCH and blocks new entries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from execution.adapter import ExecutionAdapter, ExecutionError
from lib.dates import IST
from models.execution_models import (
    RECON_MISMATCH,
    RECON_OK,
    RECON_PENDING,
    BrokerPosition,
    ReconciliationReport,
)


class PositionReconciliationService:
    """Compares internal live positions against broker-confirmed positions."""

    def __init__(self, adapter: ExecutionAdapter) -> None:
        self.adapter = adapter
        self.report = ReconciliationReport(state=RECON_PENDING, blocks_execution=True,
                                           detail="not reconciled yet")

    async def reconcile(self, internal_open: int = 0) -> ReconciliationReport:
        now = datetime.now(IST)
        try:
            positions = await self.adapter.get_positions()
        except ExecutionError as exc:
            self.report = ReconciliationReport(
                state=RECON_PENDING, checked_at=now, internal_open_positions=internal_open,
                detail=f"broker position query failed: {exc}", blocks_execution=True,
            )
            return self.report
        except Exception as exc:  # never crash the app on reconciliation
            self.report = ReconciliationReport(
                state=RECON_PENDING, checked_at=now, internal_open_positions=internal_open,
                detail=f"reconciliation error: {type(exc).__name__}", blocks_execution=True,
            )
            return self.report

        open_positions: tuple[BrokerPosition, ...] = tuple(p for p in positions if p.net_qty != 0)
        if len(open_positions) != internal_open:
            self.report = ReconciliationReport(
                state=RECON_MISMATCH, checked_at=now,
                internal_open_positions=internal_open, broker_open_positions=len(open_positions),
                unexpected_broker_positions=open_positions,
                detail=(f"internal open positions={internal_open} but broker reports "
                        f"{len(open_positions)}; new entries blocked until resolved"),
                blocks_execution=True,
            )
            return self.report

        self.report = ReconciliationReport(
            state=RECON_OK, checked_at=now, internal_open_positions=internal_open,
            broker_open_positions=len(open_positions),
            detail="internal and broker state agree", blocks_execution=False,
        )
        return self.report

    @property
    def state(self) -> str:
        return self.report.state

    def mark_pending(self, detail: str = "awaiting reconciliation") -> None:
        self.report = ReconciliationReport(
            state=RECON_PENDING, checked_at=self.report.checked_at,
            internal_open_positions=self.report.internal_open_positions,
            detail=detail, blocks_execution=True,
        )
