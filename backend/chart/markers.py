"""Phase F — mapping backend decisions to chart markers.

PARITY RULE: this module only SERIALIZES decisions produced by the Phase C
signal engine (live) or the Phase E replay engine. It contains no rule, score,
PCR, OI, VWAP or state logic of its own, and the frontend never recomputes a
signal — it renders exactly what these markers say.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from lib.dates import to_ist
from models.chart_models import (
    DEFAULT_VISIBLE_DECISIONS,
    MARKER_INVALIDATION,
    MARKER_SETUP,
    MARKER_WAIT,
    ORIGIN_LIVE,
    ORIGIN_REPLAY_SYNTHETIC,
    ChartSignalMarker,
)
from models.feature_models import MarketFeatures
from models.replay_models import ReplaySignal
from models.signal_models import SignalDecision

ORIGIN_LABELS = {
    # The live pipeline in this environment runs on the SIMULATED feed; the label
    # must never claim real market data (§8).
    ORIGIN_LIVE: "LIVE • SIMULATED DATA",
    ORIGIN_REPLAY_SYNTHETIC: "REPLAY • SYNTHETIC DATA",
}

SETUP_DECISIONS = ("CE_SETUP", "PE_SETUP")
INVALIDATION_DECISIONS = ("CE_INVALIDATED", "PE_INVALIDATED")


def origin_label(data_origin: str) -> str:
    return ORIGIN_LABELS.get(data_origin, data_origin)


def _kind(decision: str) -> str:
    if decision in SETUP_DECISIONS:
        return MARKER_SETUP
    if decision in INVALIDATION_DECISIONS:
        return MARKER_INVALIDATION
    return MARKER_WAIT


def _side(decision: str, qualifying_side: Optional[str]) -> Optional[str]:
    if decision.startswith("CE"):
        return "CE"
    if decision.startswith("PE"):
        return "PE"
    return qualifying_side


def signal_id_for(decision: SignalDecision, data_origin: str = ORIGIN_LIVE) -> str:
    """Stable, auditable id derived from the canonical backend timestamp."""
    ts = to_ist(decision.timestamp).isoformat()
    return f"{data_origin.lower()}-{ts}-{decision.decision}"


def marker_from_decision(
    decision: SignalDecision,
    features: Optional[MarketFeatures] = None,
    data_origin: str = ORIGIN_LIVE,
    origin_signal_id: Optional[str] = None,
    signal_id: Optional[str] = None,
) -> ChartSignalMarker:
    """Serialize one backend decision (+ its feature snapshot) as a marker."""
    kind = _kind(decision.decision)
    side = _side(decision.decision, decision.qualifying_side)
    ts = to_ist(decision.timestamp)

    score = max(decision.ce_score, decision.pe_score)
    if side == "CE":
        score = decision.ce_score
    elif side == "PE":
        score = decision.pe_score

    vwap_distance = None
    atm_pcr = ce_oi = ce_oi_change = pe_oi = pe_oi_change = None
    if features is not None:
        vwap_distance = features.vwap.distance
        atm_pcr = features.pcr.atm_pcr
        ce_oi, pe_oi = features.oi.call_oi, features.oi.put_oi
        ce_oi_change, pe_oi_change = features.oi.call_oi_change, features.oi.put_oi_change
    elif decision.spot is not None and decision.vwap is not None:
        vwap_distance = round(decision.spot - decision.vwap, 2)

    health = decision.data_health or "OK"
    return ChartSignalMarker(
        signal_id=signal_id or signal_id_for(decision, data_origin),
        timestamp=ts,
        symbol=decision.symbol,
        decision=decision.decision,
        state=decision.state,
        marker_kind=kind,
        side=side,
        spot=decision.spot,
        vwap=decision.vwap,
        vwap_distance=vwap_distance,
        pcr=decision.pcr,
        atm_pcr=atm_pcr,
        pcr_trend=decision.pcr_trend,
        atm=decision.atm,
        ce_oi=ce_oi, ce_oi_change=ce_oi_change,
        pe_oi=pe_oi, pe_oi_change=pe_oi_change,
        score=score,
        ce_score=decision.ce_score,
        pe_score=decision.pe_score,
        max_score=decision.ce_max_score or 100,
        reason=" | ".join(decision.reasons),
        data_health=health,
        stale="STALE" in health.upper(),
        strategy_version=decision.strategy_version,
        feature_version=decision.feature_engine_version,
        config_version=decision.config_version,
        data_origin=data_origin,
        data_origin_label=origin_label(data_origin),
        origin_signal_id=origin_signal_id if kind == MARKER_INVALIDATION else None,
        invalidation_timestamp=ts if kind == MARKER_INVALIDATION else None,
        invalidation_reason=" | ".join(decision.reasons) if kind == MARKER_INVALIDATION else None,
    )


def marker_from_replay_signal(
    signal: ReplaySignal,
    origin_signal_id: Optional[str] = None,
    data_origin: str = ORIGIN_REPLAY_SYNTHETIC,
) -> ChartSignalMarker:
    """Serialize one Phase E replay signal (same engines, offline) as a marker."""
    kind = _kind(signal.decision)
    side = _side(signal.decision, None)
    ts = to_ist(signal.timestamp)
    score = signal.ce_score if side == "CE" else signal.pe_score if side == "PE" else max(
        signal.ce_score, signal.pe_score
    )
    distance = (
        round(signal.spot - signal.vwap, 2)
        if signal.spot is not None and signal.vwap is not None
        else None
    )
    health = signal.data_health or "OK"
    return ChartSignalMarker(
        signal_id=signal.signal_id,
        timestamp=ts,
        symbol=signal.instrument,
        decision=signal.decision,
        state=signal.state,
        marker_kind=kind,
        side=side,
        spot=signal.spot,
        vwap=signal.vwap,
        vwap_distance=distance,
        pcr=signal.total_pcr,
        atm_pcr=None,  # not carried by the Phase E audit record; never invented
        pcr_trend=signal.pcr_trend,
        atm=signal.atm,
        ce_oi=signal.call_oi,
        pe_oi=signal.put_oi,
        score=score,
        ce_score=signal.ce_score,
        pe_score=signal.pe_score,
        max_score=signal.ce_max_score or 100,
        reason=signal.reason,
        data_health=health,
        stale="STALE" in health.upper(),
        strategy_version=signal.strategy_version,
        feature_version=signal.feature_engine_version,
        config_version=signal.config_version,
        data_origin=data_origin,
        data_origin_label=origin_label(data_origin),
        origin_signal_id=origin_signal_id if kind == MARKER_INVALIDATION else None,
        invalidation_timestamp=ts if kind == MARKER_INVALIDATION else None,
        invalidation_reason=signal.reason if kind == MARKER_INVALIDATION else None,
    )


def link_invalidations(markers: Sequence[ChartSignalMarker]) -> list[ChartSignalMarker]:
    """Attach each invalidation to the preceding setup's signal_id (chronological).

    The original setup marker is preserved untouched — invalidation is an extra
    event, never a deletion (§4).
    """
    ordered = sorted(markers, key=lambda m: m.timestamp)
    out: list[ChartSignalMarker] = []
    last_setup: Optional[str] = None
    for marker in ordered:
        if marker.marker_kind == MARKER_SETUP:
            last_setup = marker.signal_id
            out.append(marker)
        elif marker.marker_kind == MARKER_INVALIDATION:
            out.append(marker.model_copy(update={"origin_signal_id": marker.origin_signal_id or last_setup}))
        else:
            out.append(marker)
    return out


def visible_markers(
    markers: Sequence[ChartSignalMarker], include_wait: bool = False
) -> list[ChartSignalMarker]:
    """Default-visible markers only: CE_SETUP, PE_SETUP and invalidations."""
    ordered = sorted(markers, key=lambda m: m.timestamp)
    if include_wait:
        return ordered
    return [m for m in ordered if m.decision in DEFAULT_VISIBLE_DECISIONS]


def latest_timestamp(markers: Sequence[ChartSignalMarker]) -> Optional[datetime]:
    return max((m.timestamp for m in markers), default=None)
