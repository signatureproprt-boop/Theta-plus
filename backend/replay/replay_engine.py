"""Phase E — replay engine: the SAME Phase B/C engines, offline (§1/§18).

No look-ahead (§2): at snapshot T the engine sees only snapshots <= T. Bars,
PCR history and the previous snapshot are accumulated strictly forward, and the
session resets at each new session date (§8) so yesterday's VWAP never leaks in.

Ordering (§6): input is sorted deterministically by (timestamp, snapshot_id);
with `replay_strict_ordering` the engine REJECTS out-of-order input instead.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Iterable, Optional

from engines.feature_engine import build_features
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.vwap_engine import Bar
from lib.dates import parse_hhmm, to_ist
from models.market_models import MarketSnapshot
from models.replay_models import ReplaySignal


class ReplayOrderingError(ValueError):
    """Out-of-order snapshots under strict ordering (documented behavior)."""


class ReplayFeed:
    """Chronological snapshot emitter. Never reorders using future information —
    it only sorts by the snapshots' own (timestamp, snapshot_id)."""

    def __init__(self, snapshots: Iterable[MarketSnapshot], strict: bool = False) -> None:
        raw = list(snapshots)
        self.strict = strict
        self.ordering = "sorted"
        if strict:
            for prev, nxt in zip(raw, raw[1:]):
                if to_ist(nxt.timestamp) < to_ist(prev.timestamp):
                    raise ReplayOrderingError(
                        f"snapshot {nxt.snapshot_id} at {nxt.timestamp.isoformat()} precedes "
                        f"{prev.snapshot_id} at {prev.timestamp.isoformat()} (strict ordering)"
                    )
            self._snapshots = raw
        else:
            self._snapshots = sorted(raw, key=lambda s: (to_ist(s.timestamp), s.snapshot_id))

    def __iter__(self):
        return iter(self._snapshots)

    def __len__(self) -> int:
        return len(self._snapshots)


def _session_bar(snapshot: MarketSnapshot) -> Bar:
    t = snapshot.tick
    return Bar(high=t.high, low=t.low, close=t.close, volume=float(t.volume))


def replay_signals(
    snapshots: Iterable[MarketSnapshot],
    config,
    strict_ordering: Optional[bool] = None,
) -> tuple[list[ReplaySignal], str, str]:
    """Replay a snapshot series -> (signals, replay_hash, ordering).

    One SignalMemory per session date, so cooldown/state semantics match live
    exactly (§17) while sessions stay independent.
    """
    strict = config.replay_strict_ordering if strict_ordering is None else strict_ordering
    feed = ReplayFeed(snapshots, strict=strict)

    signals: list[ReplaySignal] = []
    session: Optional[str] = None
    bars: list[Bar] = []
    pcr_hist: list[tuple[datetime, float]] = []
    prev_snapshot: Optional[MarketSnapshot] = None
    memory = SignalMemory()
    start_t = parse_hhmm(config.signal_start_time)

    for snap in feed:
        now = to_ist(snap.timestamp, config.timezone)
        date_iso = now.date().isoformat()
        if date_iso != session:  # session reset (§8): no cross-day VWAP/PCR carry
            session, bars, pcr_hist, prev_snapshot = date_iso, [], [], None
            memory = SignalMemory()

        if not config.replay_include_pre_signal_data and now.time() < start_t:
            continue  # configured to skip pre-window data entirely

        bars.append(_session_bar(snap))  # only data at or before T
        features = build_features(
            snap, config, bars=bars, pcr_history=list(pcr_hist), prev_snapshot=prev_snapshot
        )
        decision = evaluate_and_apply(memory, features, config, now)

        if features.pcr.total_pcr is not None:
            pcr_hist.append((features.timestamp, features.pcr.total_pcr))
        prev_snapshot = snap

        signals.append(ReplaySignal(
            signal_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snap.snapshot_id}:{now.isoformat()}")),
            snapshot_id=snap.snapshot_id,
            timestamp=now,
            instrument=snap.instrument,
            spot=features.spot,
            vwap=features.vwap.vwap,
            total_pcr=features.pcr.total_pcr,
            pcr_trend=features.pcr.pcr_trend.value,
            put_oi=features.oi.put_oi,
            call_oi=features.oi.call_oi,
            ce_score=decision.ce_score,
            pe_score=decision.pe_score,
            ce_max_score=decision.ce_max_score,
            pe_max_score=decision.pe_max_score,
            decision=decision.decision,
            state=decision.state,
            reason=" | ".join(decision.reasons),
            atm=features.atm.atm_strike,
            above_vwap=features.vwap.above_vwap,
            below_vwap=features.vwap.below_vwap,
            data_health=features.data_health.status.value,
            strategy_version=decision.strategy_version,
            feature_engine_version=decision.feature_engine_version,
            config_version=decision.config_version,
            rule_results=tuple(
                r.model_dump() for r in (*decision.ce_rules, *decision.pe_rules)
            ),
        ))

    return signals, replay_hash(signals), feed.ordering


def replay_hash(signals: Iterable[ReplaySignal]) -> str:
    """Deterministic checksum over the decision-relevant fields (§10)."""
    h = hashlib.sha256()
    for s in signals:
        h.update(
            f"{s.timestamp.isoformat()}|{s.snapshot_id}|{s.decision}|{s.state}|"
            f"{s.ce_score}|{s.pe_score}|{s.total_pcr}|{s.vwap}|{s.spot}|{s.data_health}\n".encode()
        )
    return h.hexdigest()
