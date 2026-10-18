"""Phase E — metrics, segmented analysis, reports and CSV export (§19-§30).

Descriptive statistics only: every rate has an explicit denominator, nothing is
called "accuracy", no time window is ranked "best", and CE/PE are never merged
into a single misleading number.
"""

from __future__ import annotations

import csv
import io
from statistics import mean, median
from typing import Optional, Sequence

from lib.dates import parse_hhmm, to_ist
from models.replay_models import (
    BACKTEST_METHODOLOGY_VERSION,
    CostAssumptions,
    DataQualityStats,
    OUTCOME_AMBIGUOUS,
    OUTCOME_EXPIRED,
    OUTCOME_INVALIDATED,
    OUTCOME_NO_DATA,
    OUTCOME_STOPLOSS,
    OUTCOME_TARGET,
    ReplayMetrics,
    ReplaySignal,
    SegmentStats,
    SignalOutcome,
)

SETUPS = ("CE_SETUP", "PE_SETUP")


def _avg(values: Sequence[float]) -> Optional[float]:
    return round(mean(values), 4) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    return round(median(values), 4) if values else None


def _segment(label: str, signals: Sequence[ReplaySignal], outcomes: Sequence[SignalOutcome]) -> SegmentStats:
    ids = {s.signal_id for s in signals}
    seg_out = [o for o in outcomes if o.signal_id in ids]
    mfes = [o.mfe for o in seg_out if o.mfe is not None]
    maes = [o.mae for o in seg_out if o.mae is not None]
    return SegmentStats(
        label=label,
        signals=len(signals),
        ce_signals=sum(1 for s in signals if s.decision == "CE_SETUP"),
        pe_signals=sum(1 for s in signals if s.decision == "PE_SETUP"),
        target=sum(1 for o in seg_out if o.exit_state == OUTCOME_TARGET),
        stoploss=sum(1 for o in seg_out if o.exit_state == OUTCOME_STOPLOSS),
        ambiguous=sum(1 for o in seg_out if o.exit_state == OUTCOME_AMBIGUOUS),
        invalidated=sum(1 for o in seg_out if o.exit_state == OUTCOME_INVALIDATED),
        expired=sum(1 for o in seg_out if o.exit_state == OUTCOME_EXPIRED),
        no_data=sum(1 for o in seg_out if o.exit_state == OUTCOME_NO_DATA),
        avg_mfe=_avg(mfes),
        avg_mae=_avg(maes),
    )


def _window_segments(signals: Sequence[ReplaySignal], outcomes: Sequence[SignalOutcome], windows: Sequence[str]):
    segments = []
    for window in windows:
        start_s, end_s = window.split("-")
        start, end = parse_hhmm(start_s), parse_hhmm(end_s)
        in_window = [s for s in signals if start <= to_ist(s.timestamp).time() < end]
        segments.append(_segment(window, in_window, outcomes))
    return tuple(segments)


def compute_metrics(
    signals: Sequence[ReplaySignal],
    outcomes: Sequence[SignalOutcome],
    config,
    data_origin: str = "SYNTHETIC",
) -> ReplayMetrics:
    setups = [s for s in signals if s.decision in SETUPS]
    mfes = [o.mfe for o in outcomes if o.mfe is not None]
    maes = [o.mae for o in outcomes if o.mae is not None]
    t_target = [o.time_to_target_seconds for o in outcomes if o.time_to_target_seconds is not None]
    t_stop = [o.time_to_stop_seconds for o in outcomes if o.time_to_stop_seconds is not None]
    gross = [o.gross_points for o in outcomes if o.gross_points is not None]

    target_n = sum(1 for o in outcomes if o.exit_state == OUTCOME_TARGET)
    stop_n = sum(1 for o in outcomes if o.exit_state == OUTCOME_STOPLOSS)
    resolved = target_n + stop_n

    ce = [s for s in setups if s.decision == "CE_SETUP"]
    pe = [s for s in setups if s.decision == "PE_SETUP"]

    near = config.vwap_near_points
    vwap_segments = (
        _segment("above VWAP", [s for s in setups if s.above_vwap], outcomes),
        _segment("below VWAP", [s for s in setups if s.below_vwap], outcomes),
        _segment("near VWAP", [s for s in setups if s.vwap is not None and s.spot is not None
                               and abs(s.spot - s.vwap) <= near], outcomes),
        _segment("far from VWAP", [s for s in setups if s.vwap is not None and s.spot is not None
                                   and abs(s.spot - s.vwap) > near], outcomes),
    )

    quality = DataQualityStats(
        total_snapshots=len(signals),
        valid_snapshots=sum(1 for s in signals if s.data_health == "OK"),
        invalid_snapshots=sum(1 for s in signals if s.data_health == "INVALID"),
        stale_snapshots=sum(1 for s in signals if s.data_health == "STALE"),
        missing_option_data=sum(1 for s in signals if s.data_health == "INCOMPLETE"),
        missing_vwap_data=sum(1 for s in signals if s.vwap is None),
        signals_suppressed_by_data=sum(
            1 for s in signals if s.decision == "WAIT" and s.data_health != "OK"
        ),
    )

    return ReplayMetrics(
        total_evaluations=len(signals),
        total_signals=len(setups),
        ce_signals=len(ce),
        pe_signals=len(pe),
        wait_decisions=sum(1 for s in signals if s.decision == "WAIT"),
        invalidation_decisions=sum(1 for s in signals if s.decision.endswith("_INVALIDATED")),
        target=target_n,
        stoploss=stop_n,
        invalidated=sum(1 for o in outcomes if o.exit_state == OUTCOME_INVALIDATED),
        expired=sum(1 for o in outcomes if o.exit_state == OUTCOME_EXPIRED),
        ambiguous=sum(1 for o in outcomes if o.exit_state == OUTCOME_AMBIGUOUS),
        no_data=sum(1 for o in outcomes if o.exit_state == OUTCOME_NO_DATA),
        avg_mfe=_avg(mfes), avg_mae=_avg(maes),
        median_mfe=_median(mfes), median_mae=_median(maes),
        avg_time_to_target_seconds=_avg(t_target),
        avg_time_to_stop_seconds=_avg(t_stop),
        gross_points_total=round(sum(gross), 2) if gross else None,
        resolved_outcomes=resolved,
        target_rate_of_resolved=round(target_n / resolved, 4) if resolved else None,
        stop_rate_of_resolved=round(stop_n / resolved, 4) if resolved else None,
        by_side=(_segment("CE", ce, outcomes), _segment("PE", pe, outcomes)),
        by_time_window=_window_segments(setups, outcomes, config.backtest_time_windows),
        by_pcr_regime=(
            _segment("PCR rising", [s for s in setups if s.pcr_trend == "UP"], outcomes),
            _segment("PCR falling", [s for s in setups if s.pcr_trend == "DOWN"], outcomes),
            _segment("PCR flat", [s for s in setups if s.pcr_trend == "FLAT"], outcomes),
        ),
        by_vwap_regime=vwap_segments,
        data_quality=quality,
        costs=CostAssumptions(
            brokerage_per_trade=config.backtest_brokerage_per_trade,
            slippage_points=config.backtest_slippage_points,
            charges=config.backtest_charges,
            quantity=config.backtest_quantity,
            label=(
                "HYPOTHETICAL: no brokerage/slippage/charges assumed; gross points only"
                if (config.backtest_brokerage_per_trade == 0
                    and config.backtest_slippage_points == 0
                    and config.backtest_charges == 0)
                else "cost-adjusted using the configured research assumptions"
            ),
        ),
        strategy_version=config.strategy_version,
        feature_engine_version=config.feature_engine_version,
        methodology_version=BACKTEST_METHODOLOGY_VERSION,
        data_origin=data_origin,
    )


def daily_report(date_iso: str, metrics: ReplayMetrics) -> dict:
    """Machine-readable daily replay report (§28)."""
    return {
        "report": "PCR SYSTEM — REPLAY REPORT",
        "date": date_iso,
        "data_origin": metrics.data_origin,
        "evaluations": metrics.total_evaluations,
        "signals": metrics.total_signals,
        "ce": metrics.ce_signals,
        "pe": metrics.pe_signals,
        "wait": metrics.wait_decisions,
        "target": metrics.target,
        "stop": metrics.stoploss,
        "invalidated": metrics.invalidated,
        "expired": metrics.expired,
        "ambiguous": metrics.ambiguous,
        "no_data": metrics.no_data,
        "avg_mfe": metrics.avg_mfe,
        "avg_mae": metrics.avg_mae,
        "data_quality": metrics.data_quality.model_dump(),
        "costs": metrics.costs.model_dump(),
        "strategy_version": metrics.strategy_version,
        "methodology_version": metrics.methodology_version,
    }


def range_report(date_from: str, date_to: str, metrics: ReplayMetrics) -> dict:
    """Weekly / arbitrary-range aggregate report (§29)."""
    report = daily_report(f"{date_from}..{date_to}", metrics)
    report |= {
        "report": "PCR SYSTEM — REPLAY RANGE REPORT",
        "date_from": date_from,
        "date_to": date_to,
        "median_mfe": metrics.median_mfe,
        "median_mae": metrics.median_mae,
        "avg_time_to_target_seconds": metrics.avg_time_to_target_seconds,
        "avg_time_to_stop_seconds": metrics.avg_time_to_stop_seconds,
        "resolved_outcomes": metrics.resolved_outcomes,
        "target_rate_of_resolved": metrics.target_rate_of_resolved,
        "stop_rate_of_resolved": metrics.stop_rate_of_resolved,
        "rate_definition": metrics.rate_definition,
        "by_side": [s.model_dump() for s in metrics.by_side],
        "by_time_window": [s.model_dump() for s in metrics.by_time_window],
        "by_pcr_regime": [s.model_dump() for s in metrics.by_pcr_regime],
        "by_vwap_regime": [s.model_dump() for s in metrics.by_vwap_regime],
    }
    return report


def _csv(rows: Sequence[dict], headers: Sequence[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(headers), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


SIGNAL_CSV_HEADERS = [
    "signal_id", "snapshot_id", "timestamp", "instrument", "spot", "vwap", "total_pcr",
    "pcr_trend", "put_oi", "call_oi", "ce_score", "ce_max_score", "pe_score", "pe_max_score",
    "decision", "state", "atm", "data_health", "strategy_version", "feature_engine_version",
    "config_version", "reason",
]
OUTCOME_CSV_HEADERS = [
    "signal_id", "timestamp", "side", "strike", "entry", "target", "stoploss", "exit_price",
    "exit_state", "exit_timestamp", "mfe", "mae", "gross_points", "observations_used",
    "time_to_target_seconds", "time_to_stop_seconds", "time_to_invalidation_seconds",
    "methodology_version", "note",
]


def signals_csv(signals: Sequence[ReplaySignal]) -> str:
    return _csv([s.model_dump(mode="json") for s in signals], SIGNAL_CSV_HEADERS)


def outcomes_csv(outcomes: Sequence[SignalOutcome]) -> str:
    return _csv([o.model_dump(mode="json") for o in outcomes], OUTCOME_CSV_HEADERS)


def metrics_csv(metrics: ReplayMetrics) -> str:
    flat = {k: v for k, v in metrics.model_dump(mode="json").items() if not isinstance(v, (list, tuple, dict))}
    return _csv([flat], list(flat.keys()))
