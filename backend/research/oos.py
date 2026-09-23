"""Phase J — chronological out-of-sample split (§10).

The strategy is FROZEN, so this is not an optimization exercise: the identical
frozen rules run over three chronological windows and each window's measured
statistics are reported separately. Nothing is tuned on any window.
"""

from __future__ import annotations

from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict

from models.market_models import MarketSnapshot
from models.research_models import STATUS_PENDING, ExtraAnalytics
from replay.backtest_engine import run_replay
from research.analytics import compute_analytics
from research.ingest import in_memory_source

MIN_DAYS_FOR_OOS = 15  # below this a 60/20/20 split is not worth reporting


class SplitResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    date_from: str = ""
    date_to: str = ""
    trading_days: int = 0
    snapshots: int = 0
    total_signals: int = 0
    ce_signals: int = 0
    pe_signals: int = 0
    target: int = 0
    stoploss: int = 0
    invalidated: int = 0
    expired: int = 0
    ambiguous: int = 0
    no_data: int = 0
    resolved: int = 0
    target_rate_of_resolved: Optional[float] = None
    gross_points: Optional[float] = None
    net_points: Optional[float] = None
    max_drawdown_points: Optional[float] = None
    replay_hash: str = ""
    analytics: Optional[ExtraAnalytics] = None


class OosReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = STATUS_PENDING
    data_origin: str = ""
    trading_days: int = 0
    split_ratios: tuple[float, float, float] = (0.6, 0.2, 0.2)
    train: Optional[SplitResult] = None
    validation: Optional[SplitResult] = None
    out_of_sample: Optional[SplitResult] = None
    note: str = ""


def split_by_day(
    snapshots: Sequence[MarketSnapshot],
    ratios: tuple[float, float, float] = (0.6, 0.2, 0.2),
) -> tuple[list[MarketSnapshot], list[MarketSnapshot], list[MarketSnapshot]]:
    """Chronological split on whole trading days. No shuffling, ever."""
    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    days = sorted({s.timestamp.date() for s in ordered})
    n = len(days)
    train_end = int(n * ratios[0])
    val_end = train_end + int(n * ratios[1])
    train_days = set(days[:train_end])
    val_days = set(days[train_end:val_end])
    oos_days = set(days[val_end:])
    return (
        [s for s in ordered if s.timestamp.date() in train_days],
        [s for s in ordered if s.timestamp.date() in val_days],
        [s for s in ordered if s.timestamp.date() in oos_days],
    )


def _evaluate(name: str, snapshots: Sequence[MarketSnapshot], config, data_origin: str) -> SplitResult:
    if not snapshots:
        return SplitResult(name=name)
    result = run_replay(in_memory_source(snapshots), config, data_origin=data_origin)
    analytics = compute_analytics(result.signals, result.outcomes, config, data_origin)
    m = result.metrics
    days = sorted({s.timestamp.date().isoformat() for s in snapshots})
    return SplitResult(
        name=name, date_from=days[0], date_to=days[-1], trading_days=len(days),
        snapshots=len(snapshots),
        total_signals=m.total_signals, ce_signals=m.ce_signals, pe_signals=m.pe_signals,
        target=m.target, stoploss=m.stoploss, invalidated=m.invalidated,
        expired=m.expired, ambiguous=m.ambiguous, no_data=m.no_data,
        resolved=m.resolved_outcomes, target_rate_of_resolved=m.target_rate_of_resolved,
        gross_points=analytics.gross_points_total, net_points=analytics.net_points_total,
        max_drawdown_points=analytics.max_drawdown_points,
        replay_hash=result.replay_hash, analytics=analytics,
    )


def run_oos(snapshots: Sequence[MarketSnapshot], config, data_origin: str) -> OosReport:
    days = sorted({s.timestamp.date() for s in snapshots})
    if len(days) < MIN_DAYS_FOR_OOS:
        return OosReport(
            status=STATUS_PENDING, data_origin=data_origin, trading_days=len(days),
            note=(
                f"OOS = PENDING: only {len(days)} trading day(s) available; a chronological "
                f"60/20/20 split needs at least {MIN_DAYS_FOR_OOS} genuine trading days. "
                "No split was fabricated."
            ),
        )
    train, val, oos = split_by_day(snapshots)
    return OosReport(
        status="PASS", data_origin=data_origin, trading_days=len(days),
        train=_evaluate("TRAIN", train, config, data_origin),
        validation=_evaluate("VALIDATION", val, config, data_origin),
        out_of_sample=_evaluate("OUT_OF_SAMPLE", oos, config, data_origin),
        note=(
            "Chronological split only (no shuffle). The strategy is frozen, so no window "
            "was tuned and the OOS window was never used to select anything."
        ),
    )
