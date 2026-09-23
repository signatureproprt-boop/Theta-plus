"""Phase I — leakage, session-reset and determinism audits (§11/§12/§32).

All three audits are read-only: they re-run the EXISTING Phase E replay engine
and inspect its output. No strategy value is touched.
"""

from __future__ import annotations

from typing import Sequence

from models.market_models import MarketSnapshot
from models.replay_models import ReplaySignal, SignalOutcome
from models.research_models import DeterminismAudit, LeakageAudit, SessionResetAudit
from replay.backtest_engine import run_replay
from replay.replay_engine import replay_hash, replay_signals
from research.ingest import dataset_hash, in_memory_source

SETUPS = ("CE_SETUP", "PE_SETUP")


def audit_leakage(
    signals: Sequence[ReplaySignal],
    outcomes: Sequence[SignalOutcome],
    snapshots: Sequence[MarketSnapshot],
) -> LeakageAudit:
    """A signal at T may only use data with timestamp <= T; its outcome may only
    use observations with timestamp > T."""
    by_id = {s.snapshot_id: s for s in snapshots}
    future_features = 0
    for sig in signals:
        snap = by_id.get(sig.snapshot_id)
        if snap is not None and snap.timestamp > sig.timestamp:
            future_features += 1

    outcomes_by_id = {o.signal_id: o for o in outcomes}
    future_inputs = 0
    for sig in (s for s in signals if s.decision in SETUPS):
        out = outcomes_by_id.get(sig.signal_id)
        if out is None:
            continue
        if out.timestamp < sig.timestamp:
            future_inputs += 1
        if out.exit_timestamp is not None and out.exit_timestamp < sig.timestamp:
            future_inputs += 1

    passed = future_features == 0 and future_inputs == 0
    return LeakageAudit(
        checked_signals=len(signals),
        future_feature_reads=future_features,
        future_outcome_inputs=future_inputs,
        passed=passed,
        detail=(
            "no signal read data after its own timestamp; every outcome used only "
            "later observations" if passed else "FUTURE DATA ACCESS DETECTED"
        ),
    )


def audit_session_reset(snapshots: Sequence[MarketSnapshot], config) -> SessionResetAudit:
    """Replaying day-by-day must equal replaying the whole range: that only holds
    if VWAP, PCR history, signal memory and cooldown reset per day."""
    per_day: dict[str, list[MarketSnapshot]] = {}
    for snap in sorted(snapshots, key=lambda s: s.timestamp):
        per_day.setdefault(snap.timestamp.date().isoformat(), []).append(snap)
    days = sorted(per_day)

    combined = run_replay(in_memory_source(sorted(snapshots, key=lambda s: s.timestamp)), config)
    combined_by_day: dict[str, list[ReplaySignal]] = {}
    for sig in combined.signals:
        combined_by_day.setdefault(sig.timestamp.date().isoformat(), []).append(sig)

    matches = True
    counts: list[int] = []
    vwap_restarts = True
    cooldown_carryover = False
    for day in days:
        single = run_replay(in_memory_source(per_day[day]), config)
        counts.append(sum(1 for s in single.signals if s.decision in SETUPS))
        mine = [(s.decision, s.ce_score, s.pe_score, s.timestamp) for s in single.signals]
        theirs = [(s.decision, s.ce_score, s.pe_score, s.timestamp)
                  for s in combined_by_day.get(day, [])]
        if mine != theirs:
            matches = False
        first = single.signals[0] if single.signals else None
        day_open = per_day[day][0].tick.close
        if first is not None and first.vwap is not None and day_open:
            # a fresh session VWAP starts at the day's first observation
            if abs(first.vwap - day_open) > max(1.0, abs(day_open) * 0.002):
                vwap_restarts = False
        if theirs and mine and theirs[0][0] != mine[0][0]:
            cooldown_carryover = True

    passed = matches and vwap_restarts and not cooldown_carryover
    return SessionResetAudit(
        days=tuple(days),
        per_day_signal_counts=tuple(counts),
        combined_matches_per_day=matches,
        vwap_restarts=vwap_restarts,
        cooldown_carryover=cooldown_carryover,
        passed=passed,
        detail=(
            "per-day replay is identical to the combined replay: no VWAP, PCR history, "
            "signal memory or cooldown state crosses the day boundary"
            if passed else "cross-day state leakage detected"
        ),
    )


def audit_determinism(snapshots: Sequence[MarketSnapshot], config, data_origin: str) -> DeterminismAudit:
    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    h1, h2 = dataset_hash(ordered), dataset_hash(list(reversed(ordered)))
    first = run_replay(in_memory_source(ordered), config, data_origin=data_origin)
    second = run_replay(in_memory_source(ordered), config, data_origin=data_origin)
    replay_stable = (
        first.replay_hash == second.replay_hash == replay_hash(second.signals)
    )
    metrics_stable = first.metrics.model_dump() == second.metrics.model_dump()
    signals_stable = len(first.signals) == len(second.signals)
    return DeterminismAudit(
        dataset_hash_stable=h1 == h2,
        replay_hash_stable=replay_stable,
        signal_count_stable=signals_stable,
        metrics_stable=metrics_stable,
        replay_hash=first.replay_hash,
        passed=bool(h1 == h2 and replay_stable and signals_stable and metrics_stable),
    )


# --------------------------------------------------------------------------
# Phase J — mutation-based future-leakage proof (§7) and 3-run determinism (§8)
# --------------------------------------------------------------------------
def _mutate_after(snapshots: Sequence[MarketSnapshot], cut, kind: str) -> list[MarketSnapshot]:
    """Return the series with every snapshot AFTER `cut` distorted. Data at or
    before `cut` is untouched, so a leak-free engine must be unaffected."""
    out: list[MarketSnapshot] = []
    for snap in snapshots:
        if snap.timestamp <= cut:
            out.append(snap)
            continue
        tick = snap.tick
        if kind in ("price", "candle"):
            tick = tick.model_copy(update={
                "open": tick.open * 1.10, "high": tick.high * 1.15,
                "low": tick.low * 0.90, "close": tick.close * 1.12,
            })
        if kind == "volume":
            tick = tick.model_copy(update={"volume": int(tick.volume * 7) + 999})
        rows = []
        for row in snap.chain.rows:
            ce, pe = row.ce, row.pe
            if kind in ("oi", "pcr"):
                ce = ce.model_copy(update={"oi": (ce.oi or 0) * 3 + 11,
                                           "oi_change": (ce.oi_change or 0) * 5 - 77})
                pe = pe.model_copy(update={"oi": (pe.oi or 0) * 9 + 13,
                                           "oi_change": (pe.oi_change or 0) * 7 + 55})
            if kind == "ltp":
                ce = ce.model_copy(update={"ltp": (ce.ltp or 0) * 4 + 17})
                pe = pe.model_copy(update={"ltp": (pe.ltp or 0) * 6 + 23})
            rows.append(row.model_copy(update={"ce": ce, "pe": pe}))
        out.append(snap.model_copy(update={
            "tick": tick, "chain": snap.chain.model_copy(update={"rows": tuple(rows)}),
        }))
    return out


MUTATIONS = ("price", "candle", "oi", "pcr", "ltp", "volume")


def audit_mutation_leakage(snapshots: Sequence[MarketSnapshot], config) -> LeakageAudit:
    """Strongest leakage proof: distort every input AFTER T and require the
    decision at T (and all earlier ones) to stay byte-identical."""
    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    baseline, _, _ = replay_signals(ordered, config)
    if len(baseline) < 3:
        return LeakageAudit(
            checked_signals=len(baseline), passed=False,
            detail="NO_DATA: not enough evaluations to run a mutation leakage proof",
        )

    cut_index = len(ordered) // 2
    cut = ordered[cut_index].timestamp
    prefix = [s for s in baseline if s.timestamp <= cut]
    changed = 0
    offenders: list[str] = []

    for kind in MUTATIONS:
        mutated, _, _ = replay_signals(_mutate_after(ordered, cut, kind), config)
        mutated_prefix = [s for s in mutated if s.timestamp <= cut]
        fields = ("decision", "state", "ce_score", "pe_score", "total_pcr", "vwap",
                  "atm", "put_oi", "call_oi", "pcr_trend")
        for before, after in zip(prefix, mutated_prefix):
            if any(getattr(before, f) != getattr(after, f) for f in fields):
                changed += 1
                offenders.append(f"{kind}@{after.timestamp.isoformat()}")
                break

    passed = changed == 0
    return LeakageAudit(
        checked_signals=len(prefix),
        future_feature_reads=changed,
        future_outcome_inputs=0,
        passed=passed,
        detail=(
            f"mutating future {', '.join(MUTATIONS)} left all {len(prefix)} decisions at or "
            f"before {cut.isoformat()} bit-identical"
            if passed else "FUTURE DATA LEAKAGE: " + ", ".join(offenders[:5])
        ),
    )


def audit_determinism_runs(
    snapshots: Sequence[MarketSnapshot], config, data_origin: str, runs: int = 3
) -> tuple[DeterminismAudit, tuple[str, ...]]:
    """§8 — N identical runs must yield identical replay hashes and metrics."""
    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    results = [run_replay(in_memory_source(ordered), config, data_origin=data_origin)
               for _ in range(runs)]
    hashes = tuple(r.replay_hash for r in results)
    metrics_stable = all(r.metrics.model_dump() == results[0].metrics.model_dump() for r in results)
    signals_stable = all(len(r.signals) == len(results[0].signals) for r in results)
    dataset_stable = dataset_hash(ordered) == dataset_hash(list(reversed(ordered)))
    audit = DeterminismAudit(
        dataset_hash_stable=dataset_stable,
        replay_hash_stable=len(set(hashes)) == 1,
        signal_count_stable=signals_stable,
        metrics_stable=metrics_stable,
        replay_hash=hashes[0],
        passed=bool(dataset_stable and len(set(hashes)) == 1 and metrics_stable and signals_stable),
    )
    return audit, hashes
