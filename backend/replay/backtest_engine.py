"""Phase E — replay runner: source -> replay -> outcomes -> metrics -> result."""

from __future__ import annotations

from typing import Optional, Sequence

from lib.config import AppConfig, get_config
from models.market_models import MarketSnapshot
from models.replay_models import ReplayResult
from replay.metrics import compute_metrics
from replay.outcome_engine import build_outcomes
from replay.replay_engine import replay_signals
from storage.sources import HistoricalDataSource


def run_replay(
    source: HistoricalDataSource,
    config: Optional[AppConfig] = None,
    data_origin: str = "SYNTHETIC",
    strict_ordering: Optional[bool] = None,
) -> ReplayResult:
    """Full offline replay over a historical source. Strategy untouched."""
    config = config or get_config()
    snapshots: list[MarketSnapshot] = list(source.iter_snapshots())
    signals, digest, ordering = replay_signals(snapshots, config, strict_ordering)
    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    outcomes = build_outcomes(signals, ordered, config)
    metrics = compute_metrics(signals, outcomes, config, data_origin=data_origin)
    dates = sorted({s.timestamp.date().isoformat() for s in signals}) or [""]
    notes = [
        "VALIDATION ONLY: the frozen Phase A-D strategy is evaluated as-is; "
        "no optimization, tuning or ML was applied.",
    ]
    if data_origin == "SYNTHETIC":
        notes.append(
            "SYNTHETIC FIXTURE DATA — these numbers prove replay correctness only and are "
            "NOT market performance. Real-market validation: PENDING HISTORICAL DATA."
        )
    return ReplayResult(
        date_from=dates[0], date_to=dates[-1],
        instrument=snapshots[0].instrument if snapshots else config.instrument,
        signals=tuple(signals), outcomes=tuple(outcomes), metrics=metrics,
        replay_hash=digest, ordering=ordering, notes=tuple(notes),
    )
