"""Phase H — deterministic RiskEngine (§17-§25).

No AI, no ML, no probability estimation, no capital inference: every limit is
an explicit configuration value supplied by the operator. Daily loss uses
REALIZED P&L from broker-confirmed fills only — never unrealized P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lib.config import AppConfig
from models.execution_models import (
    APPROVED,
    BLOCK_MAX_DAILY_LOSS,
    BLOCK_MAX_POSITIONS,
    BLOCK_MAX_TRADES,
    BLOCK_PRICE_INVALID,
    BLOCK_QUANTITY,
    BLOCK_RISK_DISABLED,
    BLOCK_SLIPPAGE,
    RiskDecision,
)


@dataclass
class RiskState:
    """Counters the risk engine reads. Updated only from confirmed broker facts."""

    trades_today: int = 0
    open_positions: int = 0
    realized_pnl_today: float = 0.0
    locked: bool = False  # set when the daily loss limit is breached
    lock_reason: str = ""
    checks: list = field(default_factory=list)


def _finite(value) -> bool:
    return value is not None and value == value and value not in (float("inf"), float("-inf"))


class RiskEngine:
    """All limits come from `config.risk_*`; nothing is inferred."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def evaluate(
        self,
        state: RiskState,
        quantity: int,
        reference_price: float | None,
        market_price: float | None,
    ) -> RiskDecision:
        cfg = self.config
        checks: list[tuple[str, bool]] = []

        def decide(reason: str, detail: str) -> RiskDecision:
            return RiskDecision(
                approved=False, reason=reason, detail=detail, checks=tuple(checks),
                trades_today=state.trades_today, open_positions=state.open_positions,
                realized_pnl_today=state.realized_pnl_today,
            )

        if not cfg.risk_enabled:
            # Risk engine off => no live order may pass (fail closed).
            checks.append(("risk_enabled", False))
            return decide(BLOCK_RISK_DISABLED, "risk engine is disabled; live execution fails closed")
        checks.append(("risk_enabled", True))

        ok = state.trades_today < cfg.risk_max_trades_per_day
        checks.append(("max_trades_per_day", ok))
        if not ok:
            return decide(BLOCK_MAX_TRADES,
                          f"trades_today={state.trades_today} >= limit {cfg.risk_max_trades_per_day}")

        ok = state.open_positions < cfg.risk_max_open_positions
        checks.append(("max_open_positions", ok))
        if not ok:
            return decide(BLOCK_MAX_POSITIONS,
                          f"open_positions={state.open_positions} >= limit {cfg.risk_max_open_positions}")

        ok = quantity > 0 and quantity <= cfg.risk_max_quantity
        checks.append(("max_quantity", ok))
        if not ok:
            return decide(BLOCK_QUANTITY,
                          f"quantity {quantity} outside 1..{cfg.risk_max_quantity} (never auto-increased)")

        # Daily loss: realized only. max_daily_loss is a positive loss magnitude;
        # 0 means "any realized loss locks execution".
        loss = -min(state.realized_pnl_today, 0.0)
        ok = not (state.locked or loss > cfg.risk_max_daily_loss)
        checks.append(("max_daily_loss", ok))
        if not ok:
            return decide(BLOCK_MAX_DAILY_LOSS,
                          f"realized loss {loss} exceeds limit {cfg.risk_max_daily_loss}; live execution locked")

        ok = _finite(market_price) and market_price > 0
        checks.append(("price_valid", ok))
        if not ok:
            return decide(BLOCK_PRICE_INVALID, "market price is missing, zero, negative or not finite")

        if reference_price is not None:
            if not _finite(reference_price) or reference_price <= 0:
                checks.append(("price_valid_reference", False))
                return decide(BLOCK_PRICE_INVALID, "reference price is invalid")
            slippage = abs(market_price - reference_price)
            ok = slippage <= cfg.risk_max_slippage_points
            checks.append(("max_slippage_points", ok))
            if not ok:
                return decide(BLOCK_SLIPPAGE,
                              f"slippage {round(slippage, 2)} exceeds limit {cfg.risk_max_slippage_points}")

        return RiskDecision(
            approved=True, reason=APPROVED, detail="all configured risk limits satisfied",
            checks=tuple(checks), trades_today=state.trades_today,
            open_positions=state.open_positions, realized_pnl_today=state.realized_pnl_today,
        )
