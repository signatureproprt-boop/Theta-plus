"""Phase I — data quality report (§8).

A dataset is only called production-quality when every configured threshold is
met. Failures are listed explicitly; nothing is silently repaired.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from models.market_models import MarketSnapshot
from models.research_models import DataQualityReport

# Thresholds are validation gates, not strategy parameters.
MAX_DUPLICATE_RATIO = 0.0        # duplicates are never acceptable
MAX_OUT_OF_ORDER = 0             # replay requires chronological data
MAX_INVALID_RATIO = 0.0          # invalid values must not enter the engine
MAX_MISSING_RATIO = 0.05         # <=5% missing OI/LTP cells tolerated
MIN_SNAPSHOTS = 10


def _is_bad_number(value) -> bool:
    if value is None:
        return True
    try:
        f = float(value)
    except (TypeError, ValueError):
        return True
    return math.isnan(f) or math.isinf(f) or f < 0


def assess_quality(
    snapshots: Sequence[MarketSnapshot],
    dataset_id: str,
    data_origin: str,
    expected_interval_seconds: Optional[int] = None,
) -> DataQualityReport:
    failures: list[str] = []
    if not snapshots:
        return DataQualityReport(
            dataset_id=dataset_id, data_origin=data_origin, production_quality=False,
            failures=("NO_DATA: dataset contains zero snapshots",),
            note="No snapshot passed ingestion — nothing was inferred.",
        )

    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    dates = sorted({s.timestamp.date().isoformat() for s in ordered})

    seen_ids: set[str] = set()
    seen_ts: set[str] = set()
    duplicate_snapshots = duplicate_timestamps = 0
    for snap in snapshots:
        if snap.snapshot_id in seen_ids:
            duplicate_snapshots += 1
        seen_ids.add(snap.snapshot_id)
        key = snap.timestamp.isoformat()
        if key in seen_ts:
            duplicate_timestamps += 1
        seen_ts.add(key)

    out_of_order = sum(
        1 for prev, nxt in zip(snapshots, list(snapshots)[1:]) if nxt.timestamp < prev.timestamp
    )

    gaps = 0
    largest_gap: Optional[float] = None
    per_day = {}
    for snap in ordered:
        per_day.setdefault(snap.timestamp.date(), []).append(snap)
    for day_snaps in per_day.values():
        for prev, nxt in zip(day_snaps, day_snaps[1:]):
            delta = (nxt.timestamp - prev.timestamp).total_seconds()
            largest_gap = delta if largest_gap is None else max(largest_gap, delta)
            if expected_interval_seconds and delta > expected_interval_seconds * 1.5:
                gaps += 1

    missing_strikes = missing_oi = missing_ltp = invalid_values = 0
    strike_counts: list[int] = []
    expiries: set[str] = set()
    for snap in ordered:
        rows = snap.chain.rows
        strike_counts.append(len(rows))
        if not rows:
            missing_strikes += 1
        if getattr(snap.chain, "expiry", None):
            expiries.add(str(snap.chain.expiry))
        for row in rows:
            if row.ce.oi is None or row.pe.oi is None:
                missing_oi += 1
            if row.ce.ltp is None or row.pe.ltp is None:
                missing_ltp += 1
            for value in (row.ce.ltp, row.pe.ltp, row.ce.oi, row.pe.oi):
                if value is not None and _is_bad_number(value):
                    invalid_values += 1
        if _is_bad_number(snap.tick.close):
            invalid_values += 1

    cells = max(sum(strike_counts), 1)
    expected = None
    missing = None
    if expected_interval_seconds:
        expected = 0
        for day_snaps in per_day.values():
            span = (day_snaps[-1].timestamp - day_snaps[0].timestamp).total_seconds()
            expected += int(span // expected_interval_seconds) + 1
        missing = max(expected - len(ordered), 0)

    if len(ordered) < MIN_SNAPSHOTS:
        failures.append(f"snapshot_count {len(ordered)} < minimum {MIN_SNAPSHOTS}")
    if duplicate_snapshots or duplicate_timestamps:
        failures.append(
            f"duplicates present (ids={duplicate_snapshots}, timestamps={duplicate_timestamps})"
        )
    if out_of_order > MAX_OUT_OF_ORDER:
        failures.append(f"{out_of_order} out-of-order snapshots")
    if invalid_values:
        failures.append(f"{invalid_values} invalid values (negative/NaN/inf)")
    if missing_strikes:
        failures.append(f"{missing_strikes} snapshots without any option strike")
    if missing_oi / cells > MAX_MISSING_RATIO:
        failures.append(f"missing OI ratio {missing_oi / cells:.3f} > {MAX_MISSING_RATIO}")
    if missing_ltp / cells > MAX_MISSING_RATIO:
        failures.append(f"missing LTP ratio {missing_ltp / cells:.3f} > {MAX_MISSING_RATIO}")
    if gaps:
        failures.append(f"{gaps} timestamp gaps larger than 1.5x the expected interval")

    return DataQualityReport(
        dataset_id=dataset_id,
        data_origin=data_origin,
        date_from=dates[0],
        date_to=dates[-1],
        trading_days=len(dates),
        snapshot_count=len(ordered),
        expected_snapshots=expected,
        missing_snapshots=missing,
        duplicate_snapshots=duplicate_snapshots,
        duplicate_timestamps=duplicate_timestamps,
        out_of_order_snapshots=out_of_order,
        timestamp_gaps=gaps,
        largest_gap_seconds=largest_gap,
        missing_strikes=missing_strikes,
        missing_oi=missing_oi,
        missing_ltp=missing_ltp,
        invalid_values=invalid_values,
        expiry_coverage=tuple(sorted(expiries)),
        strikes_per_snapshot_min=min(strike_counts) if strike_counts else None,
        strikes_per_snapshot_max=max(strike_counts) if strike_counts else None,
        production_quality=not failures,
        failures=tuple(failures),
        note=(
            "production_quality=true only means the dataset passed these structural "
            "checks — it says nothing about strategy performance."
        ),
    )
