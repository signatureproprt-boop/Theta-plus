"""Phase I — descriptive analytics + signal evidence (§14-§25).

READ-ONLY statistics computed from the Phase E replay output. Every rate states
its denominator and every table carries N. No metric is called "accuracy" and
nothing here feeds back into the strategy.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Optional, Sequence

from models.replay_models import ReplaySignal, SignalOutcome
from models.research_models import (
    ANALYTICS_VERSION,
    Bucket,
    ExtraAnalytics,
    PeriodPnl,
    SignalEvidence,
)

SETUPS = ("CE_SETUP", "PE_SETUP")
RESOLVED_STATES = ("TARGET", "STOPLOSS")
SCORE_BUCKETS = ((0, 34), (35, 49), (50, 64), (65, 79), (80, 100))
# Phase J §11 — finer descriptive breakdowns (analytics only, strategy frozen)
SCORE_BUCKETS_FINE = ((35, 44), (45, 54), (55, 64), (65, 74), (75, 84), (85, 100))
TIME_WINDOWS_30M = ("11:30-12:00", "12:00-12:30", "12:30-13:00", "13:00-13:30",
                    "13:30-14:00", "14:00-14:30", "14:30-15:00", "15:00-15:15")


def _num(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _bucket(label: str, signals: Sequence[ReplaySignal],
            outcomes_by_id: dict[str, SignalOutcome]) -> Bucket:
    outs = [outcomes_by_id[s.signal_id] for s in signals if s.signal_id in outcomes_by_id]
    resolved = [o for o in outs if o.exit_state in RESOLVED_STATES]
    target = sum(1 for o in resolved if o.exit_state == "TARGET")
    gross = [o.gross_points for o in outs if o.gross_points is not None]
    return Bucket(
        label=label,
        n=len(signals),
        resolved=len(resolved),
        target=target,
        stoploss=sum(1 for o in resolved if o.exit_state == "STOPLOSS"),
        invalidated=sum(1 for o in outs if o.exit_state == "INVALIDATED"),
        gross_points=round(sum(gross), 2) if gross else None,
        avg_mfe=_num([o.mfe for o in outs if o.mfe is not None]),
        avg_mae=_num([o.mae for o in outs if o.mae is not None]),
        target_rate_of_resolved=round(target / len(resolved), 4) if resolved else None,
    )


def _period_pnl(label_fn, setups, outcomes_by_id, cost_per_trade: float) -> tuple[PeriodPnl, ...]:
    groups: dict[str, list[SignalOutcome]] = {}
    for sig in setups:
        out = outcomes_by_id.get(sig.signal_id)
        if out is None:
            continue
        groups.setdefault(label_fn(sig.timestamp), []).append(out)
    rows = []
    for period in sorted(groups):
        outs = groups[period]
        gross = sum(o.gross_points or 0.0 for o in outs)
        rows.append(PeriodPnl(
            period=period, n=len(outs),
            gross_points=round(gross, 2),
            net_points=round(gross - cost_per_trade * len(outs), 2),
        ))
    return tuple(rows)


def _in_window(ts: datetime, window: str) -> bool:
    start, end = window.split("-")
    hhmm = ts.strftime("%H:%M")
    return start <= hhmm < end or hhmm == end


def compute_analytics(
    signals: Sequence[ReplaySignal],
    outcomes: Sequence[SignalOutcome],
    config,
    data_origin: str,
) -> ExtraAnalytics:
    setups = [s for s in signals if s.decision in SETUPS]
    outcomes_by_id = {o.signal_id: o for o in outcomes}
    resolved = [o for o in outcomes if o.exit_state in RESOLVED_STATES]
    points = [o.gross_points for o in resolved if o.gross_points is not None]

    wins = [p for p in points if p > 0]
    losses = [p for p in points if p < 0]
    profit_factor = (
        round(sum(wins) / abs(sum(losses)), 3) if wins and losses else None
    )

    # drawdown on the cumulative gross-points curve (chronological)
    curve, peak, drawdown, cum = [], 0.0, 0.0, 0.0
    for sig in sorted(setups, key=lambda s: s.timestamp):
        out = outcomes_by_id.get(sig.signal_id)
        if out is None or out.gross_points is None:
            continue
        cum += out.gross_points
        curve.append(cum)
        peak = max(peak, cum)
        drawdown = min(drawdown, cum - peak)

    durations = [
        (out.exit_timestamp - out.timestamp).total_seconds()
        for out in outcomes if out.exit_timestamp is not None
    ]

    score_buckets = []
    for low, high in SCORE_BUCKETS:
        rows = [s for s in setups
                if low <= (s.ce_score if s.decision == "CE_SETUP" else s.pe_score) <= high]
        score_buckets.append(_bucket(f"score {low}-{high}", rows, outcomes_by_id))

    cost_per_trade = (
        config.backtest_brokerage_per_trade + config.backtest_charges
        + config.backtest_slippage_points
    )
    gross_total = round(sum(o.gross_points or 0.0 for o in outcomes), 2) if outcomes else None
    net_total = (
        round(gross_total - cost_per_trade * len(setups), 2) if gross_total is not None else None
    )

    near = config.vwap_near_points
    return ExtraAnalytics(
        sample_size_resolved=len(resolved),
        sample_size_signals=len(setups),
        gross_points_total=gross_total,
        net_points_total=net_total,
        avg_outcome_points=_num(points),
        median_outcome_points=round(median(points), 2) if points else None,
        profit_factor=profit_factor,
        expectancy_points=_num(points),
        max_drawdown_points=round(drawdown, 2) if curve else None,
        avg_signal_duration_seconds=_num(durations),
        by_score_bucket=tuple(score_buckets),
        by_score_bucket_fine=tuple(
            _bucket(f"score {low}-{high}",
                    [s for s in setups
                     if low <= (s.ce_score if s.decision == "CE_SETUP" else s.pe_score) <= high],
                    outcomes_by_id)
            for low, high in SCORE_BUCKETS_FINE
        ),
        by_time_of_day_30m=tuple(
            _bucket(win, [s for s in setups if _in_window(s.timestamp, win)], outcomes_by_id)
            for win in TIME_WINDOWS_30M
        ),
        by_time_of_day=tuple(
            _bucket(win, [s for s in setups if _in_window(s.timestamp, win)], outcomes_by_id)
            for win in config.backtest_time_windows
        ),
        by_side=(
            _bucket("CE", [s for s in setups if s.decision == "CE_SETUP"], outcomes_by_id),
            _bucket("PE", [s for s in setups if s.decision == "PE_SETUP"], outcomes_by_id),
        ),
        by_pcr_regime=(
            _bucket("PCR rising", [s for s in setups if s.pcr_trend == "UP"], outcomes_by_id),
            _bucket("PCR falling", [s for s in setups if s.pcr_trend == "DOWN"], outcomes_by_id),
            _bucket("PCR flat", [s for s in setups if s.pcr_trend == "FLAT"], outcomes_by_id),
        ),
        by_vwap_regime=(
            _bucket("above VWAP", [s for s in setups if s.above_vwap], outcomes_by_id),
            _bucket("below VWAP", [s for s in setups if s.below_vwap], outcomes_by_id),
            _bucket("near VWAP", [s for s in setups if s.vwap is not None and s.spot is not None
                                  and abs(s.spot - s.vwap) <= near], outcomes_by_id),
        ),
        by_volatility_regime=(
            _bucket("wide MFE-MAE range",
                    [s for s in setups
                     if (o := outcomes_by_id.get(s.signal_id)) is not None
                     and o.mfe is not None and o.mae is not None and (o.mfe - o.mae) >= 20],
                    outcomes_by_id),
            _bucket("narrow MFE-MAE range",
                    [s for s in setups
                     if (o := outcomes_by_id.get(s.signal_id)) is not None
                     and o.mfe is not None and o.mae is not None and (o.mfe - o.mae) < 20],
                    outcomes_by_id),
        ),
        daily_pnl=_period_pnl(lambda t: t.date().isoformat(), setups, outcomes_by_id, cost_per_trade),
        weekly_pnl=_period_pnl(lambda t: f"{t.isocalendar().year}-W{t.isocalendar().week:02d}",
                               setups, outcomes_by_id, cost_per_trade),
        monthly_pnl=_period_pnl(lambda t: t.strftime("%Y-%m"), setups, outcomes_by_id, cost_per_trade),
        analytics_version=ANALYTICS_VERSION,
        note=(
            f"Observed historical rates on {data_origin} data, N={len(resolved)} resolved "
            "outcomes. These are measured statistics, not probabilities, forecasts or accuracy."
        ),
    )


def build_evidence(
    signals: Sequence[ReplaySignal],
    outcomes: Sequence[SignalOutcome],
    data_origin: str,
    limit: int = 200,
) -> tuple[SignalEvidence, ...]:
    outcomes_by_id = {o.signal_id: o for o in outcomes}
    records = []
    for sig in [s for s in signals if s.decision in SETUPS or s.decision.endswith("_INVALIDATED")]:
        out = outcomes_by_id.get(sig.signal_id)
        records.append(SignalEvidence(
            signal_id=sig.signal_id,
            timestamp=sig.timestamp,
            decision=sig.decision,
            state=sig.state,
            spot=sig.spot,
            vwap=sig.vwap,
            vwap_distance=(round(sig.spot - sig.vwap, 2)
                           if sig.spot is not None and sig.vwap is not None else None),
            total_pcr=sig.total_pcr,
            pcr_trend=sig.pcr_trend,
            put_oi=sig.put_oi,
            call_oi=sig.call_oi,
            atm=sig.atm,
            ce_score=sig.ce_score,
            pe_score=sig.pe_score,
            max_score=sig.ce_max_score,
            reason=sig.reason,
            rule_results=sig.rule_results,
            outcome=out.exit_state if out else "NO_OUTCOME",
            exit_reason=(out.note if out else ""),
            gross_points=out.gross_points if out else None,
            mfe=out.mfe if out else None,
            mae=out.mae if out else None,
            data_health=sig.data_health,
            data_origin=data_origin,
            strategy_version=sig.strategy_version,
            feature_version=sig.feature_engine_version,
        ))
    return tuple(records[:limit])
