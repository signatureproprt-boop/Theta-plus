"""Phase J — LIVE / REPLAY parity harness (§13).

Both paths call the SAME shared engines (`build_features` + `evaluate_and_apply`)
— this module proves it by running identical snapshots through the live-pipeline
call sequence and through the Phase E replay engine and diffing every field the
strategy produces. No engine is re-implemented here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from engines.feature_engine import build_features
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.vwap_engine import Bar
from lib.config import parse_hhmm
from lib.dates import to_ist
from models.market_models import MarketSnapshot
from pydantic import BaseModel, ConfigDict
from replay.replay_engine import replay_signals

COMPARED_FIELDS = ("atm", "total_pcr", "pcr_trend", "put_oi", "call_oi", "vwap",
                   "above_vwap", "below_vwap", "ce_score", "pe_score", "decision",
                   "state", "data_health")


class ParityRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    field: str
    live_value: str
    replay_value: str


class ParityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshots: int = 0
    compared_evaluations: int = 0
    compared_fields: tuple[str, ...] = COMPARED_FIELDS
    mismatches: tuple[ParityRow, ...] = ()
    passed: bool = True
    detail: str = ""


def live_path_signals(snapshots: Sequence[MarketSnapshot], config) -> list[dict]:
    """Mirror of the live pipeline's engine call sequence (same functions, same
    order, one SignalMemory per session date)."""
    out: list[dict] = []
    session: Optional[str] = None
    bars: list[Bar] = []
    pcr_hist: list[tuple[datetime, float]] = []
    prev: Optional[MarketSnapshot] = None
    memory = SignalMemory()
    start_t = parse_hhmm(config.signal_start_time)

    for snap in sorted(snapshots, key=lambda s: s.timestamp):
        now = to_ist(snap.timestamp, config.timezone)
        date_iso = now.date().isoformat()
        if date_iso != session:
            session, bars, pcr_hist, prev = date_iso, [], [], None
            memory = SignalMemory()
        if not config.replay_include_pre_signal_data and now.time() < start_t:
            continue
        t = snap.tick
        bars.append(Bar(high=t.high, low=t.low, close=t.close, volume=float(t.volume)))
        features = build_features(
            snap, config, bars=bars, pcr_history=list(pcr_hist), prev_snapshot=prev
        )
        decision = evaluate_and_apply(memory, features, config, now)
        if features.pcr.total_pcr is not None:
            pcr_hist.append((features.timestamp, features.pcr.total_pcr))
        prev = snap
        out.append({
            "timestamp": now,
            "atm": features.atm.atm_strike,
            "total_pcr": features.pcr.total_pcr,
            "pcr_trend": features.pcr.pcr_trend.value,
            "put_oi": features.oi.put_oi,
            "call_oi": features.oi.call_oi,
            "vwap": features.vwap.vwap,
            "above_vwap": features.vwap.above_vwap,
            "below_vwap": features.vwap.below_vwap,
            "ce_score": decision.ce_score,
            "pe_score": decision.pe_score,
            "decision": decision.decision,
            "state": decision.state,
            "data_health": features.data_health.status.value,
        })
    return out


def live_replay_parity(snapshots: Sequence[MarketSnapshot], config) -> ParityReport:
    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    live = live_path_signals(ordered, config)
    replay, _, _ = replay_signals(ordered, config)

    mismatches: list[ParityRow] = []
    for lrow, rsig in zip(live, replay):
        for field in COMPARED_FIELDS:
            lval, rval = lrow[field], getattr(rsig, field)
            if lval != rval:
                mismatches.append(ParityRow(
                    timestamp=lrow["timestamp"], field=field,
                    live_value=str(lval), replay_value=str(rval),
                ))
    count_ok = len(live) == len(replay)
    passed = not mismatches and count_ok
    return ParityReport(
        snapshots=len(ordered),
        compared_evaluations=min(len(live), len(replay)),
        mismatches=tuple(mismatches[:50]),
        passed=passed,
        detail=(
            "live pipeline and Phase E replay produced identical ATM, PCR, OI, VWAP, "
            "scores, decision, state and data health for every evaluation"
            if passed else
            f"PARITY MISMATCH: {len(mismatches)} field differences"
            f"{'' if count_ok else f'; evaluation counts differ ({len(live)} vs {len(replay)})'}"
        ),
    )
