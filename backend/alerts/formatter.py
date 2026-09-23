"""Phase G — alert message formatting (deterministic backend values only).

LANGUAGE LAW: the score is a rule-confirmation score rendered "82/100" — never
a percentage, probability, "guaranteed", "sure shot" or "profit confirmed".
Every number here comes from the backend decision/features; nothing is derived
in the message layer.
"""

from __future__ import annotations

from typing import Optional

from models.chart_models import ChartSignalMarker
from models.alert_models import ALERT_INVALIDATED, ALERT_CE_SETUP, ALERT_PE_SETUP

BANNED_WORDS = ("probability", "guaranteed", "sure shot", "profit confirmed", "%")


def alert_type_for(decision: str) -> Optional[str]:
    if decision == "CE_SETUP":
        return ALERT_CE_SETUP
    if decision == "PE_SETUP":
        return ALERT_PE_SETUP
    if decision in ("CE_INVALIDATED", "PE_INVALIDATED"):
        return ALERT_INVALIDATED
    return None  # WAIT and everything else is never alerted


def _n(value, digits: int = 2) -> str:
    return "—" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def _int(value) -> str:
    return "—" if value is None else f"{value:,}"


def format_setup(marker: ChartSignalMarker, mode: str) -> str:
    side = "CE" if marker.decision.startswith("CE") else "PE"
    return "\n".join([
        f"{marker.symbol} {side} SETUP",
        "",
        f"Signal: {marker.signal_id}",
        f"Time: {marker.timestamp.strftime('%H:%M')} IST",
        "",
        f"Spot: {_n(marker.spot)}",
        f"VWAP: {_n(marker.vwap)}   (backend, strategy-authoritative)",
        f"VWAP Distance: {_n(marker.vwap_distance)}",
        "",
        f"PCR: {_n(marker.pcr)}",
        f"ATM PCR: {_n(marker.atm_pcr)}",
        f"PCR Trend: {marker.pcr_trend or '—'}",
        "",
        f"ATM: {_n(marker.atm)}",
        f"CE OI: {_int(marker.ce_oi)}   CE OI Change: {_int(marker.ce_oi_change)}",
        f"PE OI: {_int(marker.pe_oi)}   PE OI Change: {_int(marker.pe_oi_change)}",
        "",
        f"Score: {marker.score}/{marker.max_score}  (rule confirmation score, not a probability)",
        f"State: {marker.state}",
        "",
        "Reason:",
        marker.reason or "—",
        "",
        f"Mode: {mode}",
        f"Strategy: {marker.strategy_version}",
        f"Feature: {marker.feature_version}",
        f"Data: {marker.data_origin_label}",
        "",
        "Research signal only. No order was placed; no execution path exists.",
    ])


def format_invalidation(marker: ChartSignalMarker, mode: str) -> str:
    side = "CE" if marker.decision.startswith("CE") else "PE"
    return "\n".join([
        f"{marker.symbol} {side} INVALIDATED",
        "",
        f"Original Signal: {marker.origin_signal_id or '—'}",
        f"Event Signal: {marker.signal_id}",
        f"Time: {marker.timestamp.strftime('%H:%M')} IST",
        f"State: {marker.state}",
        "",
        "Reason:",
        marker.invalidation_reason or marker.reason or "—",
        "",
        f"Mode: {mode}",
        f"Data: {marker.data_origin_label}",
        "",
        "The original signal record is retained for audit; nothing was deleted.",
    ])


def format_alert(marker: ChartSignalMarker, mode: str) -> str:
    if marker.marker_kind == "INVALIDATION":
        return format_invalidation(marker, mode)
    return format_setup(marker, mode)
