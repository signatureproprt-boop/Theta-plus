"""Phase H — the SINGLE execution gate (§29/§30).

`can_execute_order()` is the only function that may authorize a broker
submission, and `execution.service.ExecutionService` is the only caller that
may act on its approval. There is no bypass path: nothing else in the
repository holds an ExecutionAdapter.

Evaluation order (all must pass, first failure wins, reason is deterministic):
mode -> execution enabled -> armed -> kill switch -> system kill switch(config)
-> signal validity -> signal state -> data health -> session -> cooldown
-> duplicate protection -> instrument/security id -> price -> risk limits
-> reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from engines.signal_engine import time_filter_reason
from execution.instruments import validate_instrument
from execution.risk_engine import RiskEngine, RiskState
from lib.config import AppConfig
from lib.dates import to_ist
from models.execution_models import (
    APPROVED,
    BLOCK_COOLDOWN,
    BLOCK_DUPLICATE,
    BLOCK_INSTRUMENT,
    BLOCK_INVALID_SIGNAL,
    BLOCK_KILL_SWITCH,
    BLOCK_LIVE_DISABLED,
    BLOCK_MODE_NOT_LIVE,
    BLOCK_NOT_ARMED,
    BLOCK_RECONCILIATION,
    BLOCK_SECURITY_ID,
    BLOCK_SESSION,
    BLOCK_SIGNAL_STATE,
    BLOCK_STALE_DATA,
    BLOCK_SYSTEM_DISABLED,
    BLOCK_UNRECONCILED_POSITION,
    MODE_LIVE,
    RECON_MISMATCH,
    RECON_OK,
    ExecutionDecision,
    InstrumentRef,
    ReconciliationReport,
)
from models.feature_models import MarketFeatures
from models.signal_models import SignalDecision

ENTRY_DECISIONS = ("CE_SETUP", "PE_SETUP")
ENTRY_STATES = ("ENTRY_CANDIDATE", "ACTIVE", "WATCH")


@dataclass(frozen=True)
class ExecutionFlags:
    """Runtime safety switches. Defaults are the SAFE defaults (§2)."""

    mode: str = "RESEARCH"
    live_execution_enabled: bool = False
    live_execution_armed: bool = False
    kill_switch: bool = False
    control_enabled: bool = False  # server-side authorization present (§46)


def can_execute_order(
    *,
    config: AppConfig,
    flags: ExecutionFlags,
    decision: Optional[SignalDecision],
    features: Optional[MarketFeatures],
    instrument: Optional[InstrumentRef],
    quantity: int,
    reference_price: Optional[float],
    market_price: Optional[float],
    risk_engine: RiskEngine,
    risk_state: RiskState,
    reconciliation: ReconciliationReport,
    existing_client_order_ids: Iterable[str],
    client_order_id: str,
    signal_id: str = "",
    signal_cooldown_until: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> ExecutionDecision:
    checks: list[tuple[str, bool]] = []
    now = to_ist(now or datetime.now())

    def blocked(reason: str, detail: str) -> ExecutionDecision:
        return ExecutionDecision(
            approved=False, reason=reason, detail=detail, checks=tuple(checks),
            client_order_id=client_order_id, signal_id=signal_id, mode=flags.mode,
        )

    # --- mode / arming / kill switch -------------------------------------
    ok = flags.mode == MODE_LIVE
    checks.append(("mode_live", ok))
    if not ok:
        return blocked(BLOCK_MODE_NOT_LIVE, f"system_mode={flags.mode}: broker execution is impossible")

    ok = flags.live_execution_enabled
    checks.append(("live_execution_enabled", ok))
    if not ok:
        return blocked(BLOCK_LIVE_DISABLED, "live_execution_enabled=false")

    ok = flags.live_execution_armed
    checks.append(("live_execution_armed", ok))
    if not ok:
        return blocked(BLOCK_NOT_ARMED, "live_execution_armed=false (explicit administrative arming required)")

    ok = not flags.kill_switch
    checks.append(("kill_switch_off", ok))
    if not ok:
        return blocked(BLOCK_KILL_SWITCH, "SYSTEM_KILL_SWITCH is ON")

    ok = config.enabled
    checks.append(("system_enabled", ok))
    if not ok:
        return blocked(BLOCK_SYSTEM_DISABLED, "strategy kill switch (config.enabled) is false")

    # --- cooldown (Phase C semantics, unchanged) -------------------------
    # A fresh setup carries the cooldown IT just started (post-apply auditability),
    # so only a cooldown that SUPPRESSED this evaluation blocks execution.
    cooldown_suppressed = any("SIGNAL_COOLDOWN" in r for r in getattr(decision, "reasons", ()))
    cooldown_active = cooldown_suppressed or (
        decision is not None
        and decision.decision not in ENTRY_DECISIONS
        and signal_cooldown_until is not None
        and to_ist(signal_cooldown_until) > now
    )
    ok = not cooldown_active
    checks.append(("cooldown_clear", ok))
    if not ok:
        return blocked(BLOCK_COOLDOWN, f"signal cooldown active until {signal_cooldown_until}")

    # --- signal validity --------------------------------------------------
    ok = decision is not None and decision.decision in ENTRY_DECISIONS
    checks.append(("signal_is_entry_setup", ok))
    if not ok:
        got = decision.decision if decision else "NO_SIGNAL"
        return blocked(BLOCK_INVALID_SIGNAL, f"decision={got}: only CE_SETUP/PE_SETUP may execute")

    ok = decision.state in ENTRY_STATES
    checks.append(("signal_state_permits_entry", ok))
    if not ok:
        return blocked(BLOCK_SIGNAL_STATE, f"state={decision.state} does not permit entry")

    # --- data health ------------------------------------------------------
    health = (decision.data_health or "").upper()
    ok = health == "OK" and features is not None
    checks.append(("data_health_ok", ok))
    if not ok:
        return blocked(BLOCK_STALE_DATA, f"data_health={health or 'NO_DATA'}")

    # --- session (reuses the existing configured session rules) ----------
    session_reason = time_filter_reason(now, config)
    ok = session_reason is None
    checks.append(("trading_session", ok))
    if not ok:
        return blocked(BLOCK_SESSION, session_reason or "outside session")

    # --- duplicate protection --------------------------------------------
    ok = client_order_id not in set(existing_client_order_ids)
    checks.append(("no_duplicate_order", ok))
    if not ok:
        return blocked(BLOCK_DUPLICATE, f"client_order_id {client_order_id} already submitted")

    # --- instrument / security id / price --------------------------------
    ok = instrument is not None and bool(instrument.security_id)
    checks.append(("security_id_available", ok))
    if not ok:
        return blocked(BLOCK_SECURITY_ID, "no validated securityId for this option; order never invented")

    valid, detail = validate_instrument(instrument, quantity, market_price)
    checks.append(("instrument_validated", valid))
    if not valid:
        return blocked(BLOCK_INSTRUMENT, detail)

    # --- risk engine ------------------------------------------------------
    risk = risk_engine.evaluate(risk_state, quantity, reference_price, market_price)
    checks.extend(risk.checks)
    if not risk.approved:
        return blocked(risk.reason, risk.detail)

    # --- reconciliation ---------------------------------------------------
    if reconciliation.state == RECON_MISMATCH and reconciliation.unexpected_broker_positions:
        checks.append(("reconciliation", False))
        return blocked(BLOCK_UNRECONCILED_POSITION, reconciliation.detail or "unexpected broker position")
    ok = reconciliation.state == RECON_OK and not reconciliation.blocks_execution
    checks.append(("reconciliation", ok))
    if not ok:
        return blocked(BLOCK_RECONCILIATION,
                       reconciliation.detail or f"reconciliation state={reconciliation.state}")

    return ExecutionDecision(
        approved=True, reason=APPROVED, detail="all safety gates and risk limits passed",
        checks=tuple(checks), client_order_id=client_order_id, signal_id=signal_id, mode=flags.mode,
    )
