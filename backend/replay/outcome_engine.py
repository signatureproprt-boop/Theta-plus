"""Phase E — outcome engine (§11-§16). Research classification, never a trade.

Uses ACTUAL historical option LTP (CE for a CE setup, PE for a PE setup) at the
signal's ATM strike — the option is never assumed to track the underlying.

Same-bar ambiguity (§13): when one observation could have touched BOTH the target
and the stop (its high >= target AND its low <= stop), ordering is unknowable at
this resolution, so the outcome is AMBIGUOUS — never the favorable side.
Observations default high=low=ltp, so ambiguity only arises with real high/low
data at that snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from lib.dates import to_ist
from models.market_models import MarketSnapshot
from models.replay_models import (
    BACKTEST_METHODOLOGY_VERSION,
    OUTCOME_AMBIGUOUS,
    OUTCOME_EXPIRED,
    OUTCOME_INVALIDATED,
    OUTCOME_NO_DATA,
    OUTCOME_STOPLOSS,
    OUTCOME_TARGET,
    ReplaySignal,
    SignalOutcome,
)


@dataclass(frozen=True)
class OptionObservation:
    """One future observation of the traded option leg."""

    timestamp: datetime
    ltp: float
    high: Optional[float] = None
    low: Optional[float] = None

    @property
    def hi(self) -> float:
        return self.high if self.high is not None else self.ltp

    @property
    def lo(self) -> float:
        return self.low if self.low is not None else self.ltp


def observations_from_snapshots(
    snapshots: Sequence[MarketSnapshot], strike: int, side: str, after: datetime
) -> list[OptionObservation]:
    """Future observations (strictly after `after`) of one option leg."""
    out: list[OptionObservation] = []
    cutoff = to_ist(after)
    for snap in snapshots:
        ts = to_ist(snap.timestamp)
        if ts <= cutoff:
            continue
        row = next((r for r in snap.chain.rows if r.strike == strike), None)
        if row is None:
            continue
        quote = row.ce if side == "CE" else row.pe
        out.append(OptionObservation(timestamp=ts, ltp=quote.ltp))
    return out


def entry_price(snapshots: Sequence[MarketSnapshot], signal: ReplaySignal) -> Optional[float]:
    """Option LTP at the signal timestamp (the research entry reference)."""
    side = "CE" if signal.decision.startswith("CE") else "PE"
    for snap in snapshots:
        if snap.snapshot_id == signal.snapshot_id:
            row = next((r for r in snap.chain.rows if r.strike == signal.atm), None)
            if row is None:
                return None
            return (row.ce if side == "CE" else row.pe).ltp
    return None


def evaluate_outcome(
    signal: ReplaySignal,
    entry: Optional[float],
    observations: Sequence[OptionObservation],
    config,
    invalidation_at: Optional[datetime] = None,
) -> SignalOutcome:
    """Classify one emitted setup against its future option observations."""
    side = "CE" if signal.decision.startswith("CE") else "PE"
    base = {
        "signal_id": signal.signal_id,
        "timestamp": signal.timestamp,
        "side": side,
        "strike": signal.atm,
        "methodology_version": BACKTEST_METHODOLOGY_VERSION,
    }
    if entry is None or entry <= 0:
        return SignalOutcome(**base, exit_state=OUTCOME_NO_DATA,
                             note="entry option LTP unavailable; no outcome invented")

    target = round(entry + config.target_points, 2)
    stop = round(entry - config.stoploss_points, 2)
    base |= {"entry": entry, "target": target, "stoploss": stop}

    if not observations:
        return SignalOutcome(**base, exit_state=OUTCOME_NO_DATA, observations_used=0,
                             note="no future option observations; no outcome invented")

    mfe = mae = 0.0
    signal_ts = to_ist(signal.timestamp)
    inv_ts = to_ist(invalidation_at) if invalidation_at else None
    used = 0

    for obs in observations:
        used += 1
        mfe = max(mfe, round(obs.hi - entry, 2))
        mae = min(mae, round(obs.lo - entry, 2))

        # Invalidation (from the replayed state machine) ends the research signal
        # before any target/stop touch that happens later.
        if inv_ts is not None and obs.timestamp > inv_ts:
            return SignalOutcome(
                **base, exit_state=OUTCOME_INVALIDATED, exit_timestamp=inv_ts,
                exit_price=None, mfe=mfe, mae=mae, observations_used=used,
                time_to_invalidation_seconds=(inv_ts - signal_ts).total_seconds(),
                note="signal invalidated by the Phase C rules before an exit touch",
            )

        hit_target = obs.hi >= target
        hit_stop = obs.lo <= stop
        if hit_target and hit_stop:
            return SignalOutcome(
                **base, exit_state=OUTCOME_AMBIGUOUS, exit_timestamp=obs.timestamp,
                mfe=mfe, mae=mae, observations_used=used,
                note="target and stop both reachable within one observation; "
                     "ordering undeterminable at this resolution (conservative: AMBIGUOUS)",
            )
        if hit_target:
            return SignalOutcome(
                **base, exit_state=OUTCOME_TARGET, exit_timestamp=obs.timestamp,
                exit_price=target, mfe=mfe, mae=mae, observations_used=used,
                gross_points=round(target - entry, 2),
                time_to_target_seconds=(obs.timestamp - signal_ts).total_seconds(),
            )
        if hit_stop:
            return SignalOutcome(
                **base, exit_state=OUTCOME_STOPLOSS, exit_timestamp=obs.timestamp,
                exit_price=stop, mfe=mfe, mae=mae, observations_used=used,
                gross_points=round(stop - entry, 2),
                time_to_stop_seconds=(obs.timestamp - signal_ts).total_seconds(),
            )

    last = observations[-1]
    return SignalOutcome(
        **base, exit_state=OUTCOME_EXPIRED, exit_timestamp=last.timestamp,
        exit_price=last.ltp, mfe=mfe, mae=mae, observations_used=used,
        gross_points=round(last.ltp - entry, 2),
        note="session data exhausted without target/stop touch",
    )


def build_outcomes(
    signals: Sequence[ReplaySignal], snapshots: Sequence[MarketSnapshot], config
) -> list[SignalOutcome]:
    """Outcomes for every emitted CE/PE setup in a replay run."""
    invalidations = [
        (to_ist(s.timestamp), "CE" if s.decision.startswith("CE") else "PE")
        for s in signals if s.decision.endswith("_INVALIDATED")
    ]
    outcomes: list[SignalOutcome] = []
    for sig in signals:
        if sig.decision not in ("CE_SETUP", "PE_SETUP") or sig.atm is None:
            continue
        side = "CE" if sig.decision.startswith("CE") else "PE"
        inv = next((ts for ts, s in invalidations if s == side and ts > to_ist(sig.timestamp)), None)
        outcomes.append(evaluate_outcome(
            sig,
            entry_price(snapshots, sig),
            observations_from_snapshots(snapshots, sig.atm, side, sig.timestamp),
            config,
            invalidation_at=inv,
        ))
    return outcomes
