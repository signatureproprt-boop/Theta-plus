"""Phase H — ExecutionService: the ONLY caller of an ExecutionAdapter.

    decision -> build order (validated) -> can_execute_order() -> adapter.submit_order

Everything else (idempotency, lifecycle, timeout reconciliation, partial fills,
EXECUTION_LOG, risk counters, exit safety) lives here. The strategy is never
touched: this service only consumes an already-produced SignalDecision.

DEFAULTS: mode RESEARCH, live execution DISABLED + DISARMED, kill switch OFF
(meaning "not engaged"), adapter = Mock. Live submission additionally requires
a completed reconciliation.
"""

from __future__ import annotations

import hashlib
import os
from collections import deque
from datetime import datetime
from typing import Optional

from execution.adapter import (
    BrokerOrderState,
    ExecutionAdapter,
    ExecutionError,
    ExecutionTimeout,
    MockExecutionAdapter,
)
from execution.gate import ExecutionFlags, can_execute_order
from execution.instruments import InstrumentMap
from execution.reconciliation import PositionReconciliationService
from execution.risk_engine import RiskEngine, RiskState
from lib.config import AppConfig
from lib.dates import IST, to_ist
from models.execution_models import (
    APPROVED,
    BLOCK_CONTROL_DISABLED,
    BLOCK_INSTRUMENT,
    BLOCK_RECONCILIATION,
    BLOCK_SECURITY_ID,
    BROKER_STATUS_MAP,
    MODE_LIVE,
    MODE_RESEARCH,
    ORDER_ACKNOWLEDGED,
    ORDER_FILLED,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    ORDER_REQUESTED,
    ORDER_SUBMITTED,
    ORDER_UNKNOWN,
    RECON_OK,
    RECON_PENDING,
    ExecutionDecision,
    ExecutionLogEntry,
    ExecutionStatusPanel,
    InstrumentRef,
    OrderRecord,
    OrderRequest,
)
from models.feature_models import MarketFeatures
from models.signal_models import SignalDecision


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def client_order_id_for(signal_id: str) -> str:
    """Deterministic idempotency key (<=30 chars, Dhan correlationId safe)."""
    digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:20]
    return f"PCR-{digest}"


class ExecutionService:
    """Additive execution/risk layer. Never modifies strategy behaviour."""

    def __init__(
        self,
        config: AppConfig,
        adapter: Optional[ExecutionAdapter] = None,
        instrument_map: Optional[InstrumentMap] = None,
    ) -> None:
        self.config = config
        self.adapter: ExecutionAdapter = adapter or MockExecutionAdapter(is_configured=False)
        self.instruments = instrument_map or InstrumentMap.from_env()
        self.risk_engine = RiskEngine(config)
        self.risk_state = RiskState()
        self.reconciler = PositionReconciliationService(self.adapter)
        self.orders: dict[str, OrderRecord] = {}
        self.log: deque[ExecutionLogEntry] = deque(maxlen=500)
        self.last_block_reason = ""
        self.kill_switch = _env_flag("SYSTEM_KILL_SWITCH")

    # --- flags -------------------------------------------------------------
    @property
    def flags(self) -> ExecutionFlags:
        """Safe defaults unless the operator set the env switches server-side."""
        return ExecutionFlags(
            mode=self.config.system_mode,
            live_execution_enabled=self.config.live_execution_enabled and _env_flag("LIVE_EXECUTION_ENABLED"),
            live_execution_armed=self.config.live_execution_armed and _env_flag("LIVE_EXECUTION_ARMED"),
            kill_switch=self.kill_switch,
            control_enabled=False,  # no auth system => no UI/API control exists (§46)
        )

    def engage_kill_switch(self) -> None:
        """Server-side only: stops all new live orders immediately."""
        self.kill_switch = True

    # --- logging -----------------------------------------------------------
    def _log(self, event: str, **fields) -> ExecutionLogEntry:
        entry = ExecutionLogEntry(timestamp=datetime.now(IST), event=event, **fields)
        self.log.append(entry)
        return entry

    def entries(self) -> list[ExecutionLogEntry]:
        return list(self.log)

    # --- order construction -----------------------------------------------
    def _resolve(self, decision: SignalDecision, features: Optional[MarketFeatures]):
        side = "CE" if decision.decision.startswith("CE") else "PE"
        strike = features.atm.atm_strike if features else decision.atm
        quote = None
        if features is not None:
            quote = features.atm.atm_ce if side == "CE" else features.atm.atm_pe
        ltp = quote.ltp if quote is not None else None
        ref: Optional[InstrumentRef] = self.instruments.resolve("NIFTY", strike, side)
        return side, strike, ltp, ref

    # --- the single execution path ----------------------------------------
    async def attempt_entry(
        self,
        decision: Optional[SignalDecision],
        features: Optional[MarketFeatures],
        signal_id: str,
        now: Optional[datetime] = None,
        reference_price: Optional[float] = None,
    ) -> ExecutionDecision:
        now = to_ist(now or datetime.now(IST))
        coid = client_order_id_for(signal_id)
        side = strike = ltp = ref = None
        if decision is not None:
            side, strike, ltp, ref = self._resolve(decision, features)

        verdict = can_execute_order(
            config=self.config,
            flags=self.flags,
            decision=decision,
            features=features,
            instrument=ref,
            quantity=self.config.live_quantity,
            reference_price=reference_price if reference_price is not None else ltp,
            market_price=ltp,
            risk_engine=self.risk_engine,
            risk_state=self.risk_state,
            reconciliation=self.reconciler.report,
            existing_client_order_ids=self.orders.keys(),
            client_order_id=coid,
            signal_id=signal_id,
            signal_cooldown_until=decision.cooldown_until if decision else None,
            now=now,
        )

        if not verdict.approved:
            self.last_block_reason = verdict.reason
            self._log(
                "EXECUTION_BLOCKED", signal_id=signal_id, client_order_id=coid,
                mode=self.flags.mode, risk_decision="BLOCKED", block_reason=verdict.reason,
                security_id=(ref.security_id if ref else ""),
                reconciliation_state=self.reconciler.state, message=verdict.detail,
            )
            return verdict

        order = OrderRequest(
            client_order_id=coid, signal_id=signal_id, instrument="NIFTY",
            exchange_segment=ref.exchange_segment, security_id=ref.security_id,
            transaction_type="BUY",  # research strategy buys the ATM option leg
            order_type=self.config.live_order_type,
            product_type=self.config.live_product_type,
            validity=self.config.live_validity,
            quantity=self.config.live_quantity,
            price=round(float(ltp), 2) if self.config.live_order_type == "LIMIT" else 0.0,
            reference_price=round(float(ltp), 2), mode=MODE_LIVE,
        )
        self._log(
            "EXECUTION_APPROVED", signal_id=signal_id, client_order_id=coid, mode=MODE_LIVE,
            security_id=order.security_id, transaction_type=order.transaction_type,
            order_type=order.order_type, quantity=order.quantity, requested_price=order.price,
            risk_decision=APPROVED, reconciliation_state=self.reconciler.state,
        )
        await self._submit(order)
        return verdict

    async def _submit(self, order: OrderRequest) -> OrderRecord:
        now = datetime.now(IST)
        record = OrderRecord(
            client_order_id=order.client_order_id, signal_id=order.signal_id,
            status=ORDER_REQUESTED, mode=MODE_LIVE, security_id=order.security_id,
            exchange_segment=order.exchange_segment, transaction_type=order.transaction_type,
            order_type=order.order_type, requested_quantity=order.quantity,
            remaining_quantity=order.quantity, requested_price=order.price,
            created_at=now, updated_at=now, reconciliation_state=self.reconciler.state,
        )
        # Persist the idempotency key BEFORE submission so a duplicate request
        # can never produce a second broker order.
        self.orders[order.client_order_id] = record
        self.risk_state.trades_today += 1

        try:
            ack = await self.adapter.submit_order(order)
        except ExecutionTimeout as exc:
            # UNKNOWN: never blindly resend — reconcile by correlationId (§34/§35).
            record = self._update(record, status=ORDER_UNKNOWN, error_message=str(exc),
                                  error_code="TIMEOUT")
            self._log("ORDER_UNKNOWN", signal_id=order.signal_id,
                      client_order_id=order.client_order_id, mode=MODE_LIVE,
                      status=ORDER_UNKNOWN, message="timeout; reconciling by correlation id")
            state = None
            try:
                state = await self.adapter.get_order_by_correlation(order.client_order_id)
            except Exception:
                state = None
            if state is not None:
                record = self._apply_broker_state(record, state)
            return record
        except ExecutionError as exc:
            record = self._update(record, status=ORDER_REJECTED, error_code="ADAPTER_ERROR",
                                  error_message=str(exc))
            self._log("ORDER_REJECTED", signal_id=order.signal_id,
                      client_order_id=order.client_order_id, mode=MODE_LIVE,
                      status=ORDER_REJECTED, error_code="ADAPTER_ERROR", message=str(exc))
            return record

        record = self._update(
            record, status=BROKER_STATUS_MAP.get(ack.broker_status, ORDER_SUBMITTED),
            broker_order_id=ack.broker_order_id, broker_status=ack.broker_status,
        )
        self._log("ORDER_SUBMITTED", signal_id=order.signal_id,
                  client_order_id=order.client_order_id, broker_order_id=ack.broker_order_id,
                  mode=MODE_LIVE, security_id=order.security_id, quantity=order.quantity,
                  requested_price=order.price, status=record.status,
                  reconciliation_state=self.reconciler.state)
        # Confirm the authoritative state (SUBMITTED is never treated as FILLED).
        try:
            state = await self.adapter.get_order(ack.broker_order_id)
            record = self._apply_broker_state(record, state)
        except Exception:
            pass
        return record

    # --- lifecycle ---------------------------------------------------------
    def _update(self, record: OrderRecord, **fields) -> OrderRecord:
        new = record.model_copy(update={**fields, "updated_at": datetime.now(IST)})
        self.orders[new.client_order_id] = new
        return new

    def _apply_broker_state(self, record: OrderRecord, state: BrokerOrderState) -> OrderRecord:
        status = BROKER_STATUS_MAP.get(state.broker_status, ORDER_UNKNOWN)
        filled = state.filled_quantity
        remaining = state.remaining_quantity or max(record.requested_quantity - filled, 0)
        if status == ORDER_FILLED and filled < record.requested_quantity and filled > 0:
            status = ORDER_PARTIALLY_FILLED  # never treat a partial as full
        record = self._update(
            record, status=status, broker_order_id=state.broker_order_id or record.broker_order_id,
            broker_status=state.broker_status, filled_quantity=filled,
            remaining_quantity=remaining, average_fill_price=state.average_traded_price,
            error_code=state.error_code, error_message=state.error_message,
        )
        self._log("ORDER_STATE_CHANGE", signal_id=record.signal_id,
                  client_order_id=record.client_order_id,
                  broker_order_id=record.broker_order_id or "", mode=record.mode,
                  status=record.status, average_fill_price=record.average_fill_price,
                  quantity=record.requested_quantity, message=f"broker status {state.broker_status}")
        return record

    async def refresh_order(self, client_order_id: str) -> Optional[OrderRecord]:
        record = self.orders.get(client_order_id)
        if record is None:
            return None
        try:
            if record.broker_order_id:
                state = await self.adapter.get_order(record.broker_order_id)
            else:
                state = await self.adapter.get_order_by_correlation(client_order_id)
        except Exception as exc:
            self._log("ORDER_RECONCILE_FAILED", client_order_id=client_order_id,
                      signal_id=record.signal_id, message=str(exc))
            return record
        if state is None:
            return record
        return self._apply_broker_state(record, state)

    # --- realized P&L (broker-confirmed fills only) ------------------------
    def record_realized_pnl(self, amount: float) -> None:
        self.risk_state.realized_pnl_today = round(self.risk_state.realized_pnl_today + amount, 2)
        loss = -min(self.risk_state.realized_pnl_today, 0.0)
        if loss > self.config.risk_max_daily_loss:
            self.risk_state.locked = True
            self.risk_state.lock_reason = "MAX_DAILY_LOSS"
            self._log("LIVE_EXECUTION_LOCKED", mode=self.flags.mode,
                      block_reason="MAX_DAILY_LOSS",
                      message=f"realized loss {loss} exceeded limit {self.config.risk_max_daily_loss}")

    # --- exit safety (§40/§41) --------------------------------------------
    async def can_exit(self, security_id: str) -> tuple[bool, str]:
        """An exit needs a broker-CONFIRMED non-zero position and clean reconciliation."""
        if self.reconciler.state != RECON_OK:
            return False, BLOCK_RECONCILIATION
        try:
            positions = await self.adapter.get_positions()
        except Exception:
            return False, BLOCK_RECONCILIATION
        position = next((p for p in positions if p.security_id == security_id), None)
        if position is None or position.net_qty == 0:
            return False, "NO_LIVE_POSITION"
        return True, APPROVED

    # --- startup reconciliation (§38) -------------------------------------
    async def startup_reconcile(self) -> str:
        if not getattr(self.adapter, "configured", False):
            self.reconciler.mark_pending("broker not configured; live execution blocked")
            self._log("RECONCILIATION", reconciliation_state=RECON_PENDING,
                      message="broker not configured")
            return RECON_PENDING
        internal_open = sum(
            1 for r in self.orders.values()
            if r.status in (ORDER_ACKNOWLEDGED, ORDER_PARTIALLY_FILLED, ORDER_FILLED)
        )
        report = await self.reconciler.reconcile(internal_open)
        self._log("RECONCILIATION", reconciliation_state=report.state, message=report.detail)
        return report.state

    # --- read-only status panel -------------------------------------------
    def status(self) -> ExecutionStatusPanel:
        flags = self.flags
        cfg = self.config
        broker = "NOT_CONFIGURED"
        if getattr(self.adapter, "configured", False):
            broker = "CONNECTED" if self.reconciler.state == RECON_OK else "DISCONNECTED"
        risk = self.risk_engine.evaluate(
            self.risk_state, cfg.live_quantity, None, 1.0
        )
        return ExecutionStatusPanel(
            system_mode=cfg.system_mode,
            live_execution_enabled=flags.live_execution_enabled,
            live_execution_armed=flags.live_execution_armed,
            kill_switch=flags.kill_switch,
            control_enabled=flags.control_enabled,
            control_note=(
                "LIVE EXECUTION CONTROL = DISABLED: no server-side authentication/role system "
                "exists, so no UI or API can enable or arm live execution. Arming is an explicit "
                "server-side operator action (env: LIVE_EXECUTION_ENABLED + LIVE_EXECUTION_ARMED)."
            ),
            risk_state="PASS" if risk.approved else "BLOCKED",
            risk_reason=risk.reason,
            broker=broker,
            broker_verified=False,
            reconciliation=self.reconciler.state,
            adapter=getattr(self.adapter, "name", "mock"),
            max_trades_per_day=cfg.risk_max_trades_per_day,
            max_open_positions=cfg.risk_max_open_positions,
            max_quantity=cfg.risk_max_quantity,
            max_daily_loss=cfg.risk_max_daily_loss,
            max_slippage_points=cfg.risk_max_slippage_points,
            quantity=cfg.live_quantity,
            trades_today=self.risk_state.trades_today,
            open_orders=sum(1 for r in self.orders.values()
                            if r.status in (ORDER_SUBMITTED, ORDER_ACKNOWLEDGED, ORDER_PARTIALLY_FILLED)),
            realized_pnl_today=self.risk_state.realized_pnl_today,
            last_block_reason=self.last_block_reason,
            orders_submitted=len(self.orders),
            confirmation_required=(
                f"MODE={cfg.system_mode}",
                f"INSTRUMENT=NIFTY {cfg.live_product_type} {cfg.live_order_type} {cfg.live_validity}",
                f"QUANTITY={cfg.live_quantity}",
                f"MAX_TRADES_PER_DAY={cfg.risk_max_trades_per_day}",
                f"MAX_OPEN_POSITIONS={cfg.risk_max_open_positions}",
                f"MAX_DAILY_LOSS={cfg.risk_max_daily_loss}",
                f"MAX_SLIPPAGE_POINTS={cfg.risk_max_slippage_points}",
                f"KILL_SWITCH={'ON' if flags.kill_switch else 'OFF'}",
                f"BROKER={broker}",
                f"RECONCILIATION={self.reconciler.state}",
            ),
            note=(
                "Live execution requires mode=LIVE plus live_execution_enabled AND "
                "live_execution_armed AND kill switch off AND a completed reconciliation. "
                "Defaults are RESEARCH / DISABLED / DISARMED. Real broker verification: PENDING."
            ),
        )
