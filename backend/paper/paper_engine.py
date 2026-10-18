"""Phase G — PaperTradingEngine.

    SignalDecision -> Paper Entry -> Paper Position -> Market Updates
                   -> Target / SL / Invalidation / Expiry -> Paper Exit -> Paper P&L

SAFETY LAWS
- No broker API, no Dhan order API, no order id, no execution adapter (§9/§28).
- Target/stop come from the EXISTING Phase C config (`target_points`,
  `stoploss_points`). No second target/SL rule system exists (§13).
- Invalidation is the BACKEND decision (`CE_INVALIDATED` / `PE_INVALIDATED`)
  produced by the frozen signal engine — the paper engine invents none (§14).
- Same-observation target AND stop reachability => AMBIGUOUS, exactly like the
  Phase E outcome engine; never the favorable side (§15).
- Missing price data => NO_DATA. No price is ever fabricated (§10/§16).
- One position per signal, `max_open_paper_positions` overall (§17).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from lib.config import AppConfig
from lib.dates import to_ist
from models.feature_models import MarketFeatures
from models.market_models import MarketSnapshot
from models.paper_models import (
    CLOSED_STATES,
    PAPER_AMBIGUOUS,
    PAPER_EXPIRED,
    PAPER_INVALIDATED,
    PAPER_LABEL,
    PAPER_NO_DATA,
    PAPER_OPEN,
    PAPER_STOPLOSS,
    PAPER_TARGET,
    PaperPosition,
    PaperSummary,
)
from models.signal_models import SignalDecision
# Reuse the Phase E observation type so live paper and replay share one shape.
from replay.outcome_engine import OptionObservation

ORIGIN_LABELS = {
    "LIVE": "LIVE • SIMULATED DATA",
    "REPLAY-SYNTHETIC": "REPLAY • SYNTHETIC DATA",
}


def observation_for(
    snapshot: Optional[MarketSnapshot], strike: Optional[int], side: str, at: datetime
) -> Optional[OptionObservation]:
    """Current option LTP for one leg, or None when the data is unavailable."""
    if snapshot is None or strike is None:
        return None
    row = next((r for r in snapshot.chain.rows if r.strike == strike), None)
    if row is None:
        return None
    quote = row.ce if side == "CE" else row.pe
    if quote is None or quote.ltp is None or quote.ltp <= 0:
        return None
    return OptionObservation(timestamp=to_ist(at), ltp=float(quote.ltp))


class PaperTradingEngine:
    """PAPER / HYPOTHETICAL position tracker. Never talks to a broker."""

    def __init__(self, config: AppConfig, data_origin: str = "LIVE") -> None:
        self.config = config
        self.data_origin = data_origin
        self.positions: list[PaperPosition] = []
        self._signal_ids: set[str] = set()

    # --- helpers ---
    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS.get(self.data_origin, self.data_origin)

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self.positions if p.is_open]

    def trades(self) -> list[PaperPosition]:
        """Closed paper trades = the PAPER_TRADE_LOG rows (§19)."""
        return [p for p in self.positions if p.state in CLOSED_STATES]

    def get(self, paper_position_id: str) -> Optional[PaperPosition]:
        return next((p for p in self.positions if p.paper_position_id == paper_position_id), None)

    def _replace(self, old: PaperPosition, new: PaperPosition) -> PaperPosition:
        self.positions = [new if p.paper_position_id == old.paper_position_id else p for p in self.positions]
        return new

    # --- entry (§10) ---
    def open_from_decision(
        self,
        decision: SignalDecision,
        features: Optional[MarketFeatures],
        signal_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[PaperPosition]:
        if decision.decision not in ("CE_SETUP", "PE_SETUP"):
            return None
        if signal_id in self._signal_ids:
            return None  # duplicate-signal / repeated-polling protection (§17)
        if len(self.open_positions()) >= self.config.max_open_paper_positions:
            return None

        side = "CE" if decision.decision == "CE_SETUP" else "PE"
        at = to_ist(now or decision.timestamp)
        strike = features.atm.atm_strike if features else decision.atm
        quote = None
        if features is not None:
            quote = features.atm.atm_ce if side == "CE" else features.atm.atm_pe
        entry = quote.ltp if quote is not None and quote.ltp and quote.ltp > 0 else None

        self._signal_ids.add(signal_id)
        if entry is None:
            # NO_DATA: recorded for audit, never an invented entry price (§10/§16).
            position = PaperPosition(
                paper_position_id=str(uuid.uuid4()), signal_id=signal_id, timestamp=at,
                underlying=decision.symbol, option_type=side, strike=strike,
                quantity=self.config.paper_quantity, state=PAPER_NO_DATA,
                exit_reason="entry option LTP unavailable; no entry invented",
                data_origin=self.data_origin, data_origin_label=self.origin_label,
                mode=self.config.system_mode, label=PAPER_LABEL,
                strategy_version=decision.strategy_version,
                feature_version=decision.feature_engine_version,
            )
            self.positions.append(position)
            return position

        position = PaperPosition(
            paper_position_id=str(uuid.uuid4()), signal_id=signal_id, timestamp=at,
            underlying=decision.symbol, option_type=side, strike=strike,
            entry_price=round(float(entry), 2), current_price=round(float(entry), 2),
            target_price=round(float(entry) + self.config.target_points, 2),
            stoploss_price=round(float(entry) - self.config.stoploss_points, 2),
            quantity=self.config.paper_quantity, state=PAPER_OPEN,
            assumed_slippage_points=self.config.paper_slippage_points,
            costs=self.config.paper_costs_per_trade,
            data_origin=self.data_origin, data_origin_label=self.origin_label,
            mode=self.config.system_mode, label=PAPER_LABEL,
            strategy_version=decision.strategy_version,
            feature_version=decision.feature_engine_version,
        )
        self.positions.append(position)
        return position

    # --- exit + P&L (§18) ---
    def _close(self, position: PaperPosition, state: str, price: Optional[float],
               at: datetime, reason: str) -> PaperPosition:
        if price is None or position.entry_price is None:
            return self._replace(position, position.model_copy(update={
                "state": state, "exit_time": to_ist(at),
                "exit_reason": reason or "exit price unavailable; no P&L invented",
            }))
        slippage = self.config.paper_slippage_points
        costs = self.config.paper_costs_per_trade
        qty = position.quantity
        gross_points = round(price - position.entry_price, 2)
        gross_pnl = round(gross_points * qty, 2)
        net_pnl = round((gross_points - slippage) * qty - costs, 2)
        return self._replace(position, position.model_copy(update={
            "state": state,
            "current_price": round(price, 2),
            "exit_price": round(price, 2),
            "exit_time": to_ist(at),
            "exit_reason": reason,
            "gross_points": gross_points,
            "gross_pnl": gross_pnl,
            "assumed_slippage_points": slippage,
            "costs": costs,
            "net_pnl": net_pnl,
        }))

    def update(
        self,
        observation: Optional[OptionObservation],
        at: datetime,
        invalidated: bool = False,
        expired: bool = False,
    ) -> Optional[PaperPosition]:
        """Advance the open position with one observation of its option leg."""
        position = next(iter(self.open_positions()), None)
        if position is None:
            return None
        at = to_ist(at)

        if observation is None:
            # Data gap: nothing is assumed. Only an explicit backend event closes.
            if invalidated:
                return self._close(position, PAPER_INVALIDATED, None, at,
                                   "backend invalidation; exit price unavailable")
            if expired:
                return self._close(position, PAPER_EXPIRED, None, at,
                                   "session end; exit price unavailable")
            return self._replace(position, position.model_copy(update={
                "exit_reason": "", "current_price": position.current_price,
            }))

        price = observation.ltp
        position = self._replace(position, position.model_copy(update={"current_price": round(price, 2)}))
        target, stop = position.target_price, position.stoploss_price

        if target is not None and stop is not None:
            hit_target = observation.hi >= target
            hit_stop = observation.lo <= stop
            if hit_target and hit_stop:
                # Phase E semantics: ordering unknowable at this resolution.
                return self._close(position, PAPER_AMBIGUOUS, price, at,
                                   "target and stop both reachable in the same observation")
            if hit_target:
                return self._close(position, PAPER_TARGET, price, at,
                                   f"option LTP reached target {target}")
            if hit_stop:
                return self._close(position, PAPER_STOPLOSS, price, at,
                                   f"option LTP reached stoploss {stop}")

        if invalidated:
            return self._close(position, PAPER_INVALIDATED, price, at,
                               "backend signal invalidation (frozen engine)")
        if expired:
            return self._close(position, PAPER_EXPIRED, price, at, "session end reached")
        return position

    # --- one contained iteration used by the live pipeline ---
    def on_tick(
        self,
        decision: SignalDecision,
        features: Optional[MarketFeatures],
        snapshot: Optional[MarketSnapshot],
        signal_id: str,
        now: Optional[datetime] = None,
        expired: bool = False,
    ) -> Optional[PaperPosition]:
        at = to_ist(now or decision.timestamp)
        invalidated = decision.decision in ("CE_INVALIDATED", "PE_INVALIDATED")
        open_position = next(iter(self.open_positions()), None)
        if open_position is not None:
            obs = observation_for(snapshot, open_position.strike, open_position.option_type, at)
            return self.update(obs, at, invalidated=invalidated, expired=expired)
        return self.open_from_decision(decision, features, signal_id, now=at)

    # --- summary (§22) ---
    def summary(self) -> PaperSummary:
        closed = self.trades()
        def count(state: str) -> int:
            return sum(1 for p in self.positions if p.state == state)

        gross_points = round(sum(p.gross_points or 0.0 for p in closed), 2)
        gross_pnl = round(sum(p.gross_pnl or 0.0 for p in closed), 2)
        costs = round(sum(p.costs or 0.0 for p in closed), 2)
        net_pnl = round(sum(p.net_pnl or 0.0 for p in closed), 2)
        zero_costs = self.config.paper_costs_per_trade == 0 and self.config.paper_slippage_points == 0
        return PaperSummary(
            mode=self.config.system_mode,
            paper_trading_enabled=self.config.paper_trading_enabled or self.config.system_mode == "PAPER",
            data_origin=self.data_origin,
            data_origin_label=self.origin_label,
            total_positions=len(self.positions),
            open_positions=len(self.open_positions()),
            target=count(PAPER_TARGET),
            stoploss=count(PAPER_STOPLOSS),
            invalidated=count(PAPER_INVALIDATED),
            expired=count(PAPER_EXPIRED),
            ambiguous=count(PAPER_AMBIGUOUS),
            no_data=count(PAPER_NO_DATA),
            gross_points=gross_points,
            gross_pnl=gross_pnl,
            costs=costs,
            net_pnl=net_pnl,
            quantity=self.config.paper_quantity,
            target_points=self.config.target_points,
            stoploss_points=self.config.stoploss_points,
            max_open_positions=self.config.max_open_paper_positions,
            cost_note=(
                "HYPOTHETICAL: assumed slippage and costs are zero, so P&L is gross of "
                "real charges."
                if zero_costs
                else "HYPOTHETICAL: slippage/costs are assumed configuration values, not broker charges."
            ),
            open_position=next(iter(self.open_positions()), None),
        )
