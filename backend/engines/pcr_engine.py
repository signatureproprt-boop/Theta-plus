"""Phase B — PCR engine: total & ATM-range PCR, change, and trend.

Deterministic and total: zero OI, missing strikes and stale chains never crash
the engine — they degrade the output and are reported through `quality`/`valid`.

PCR is a CONFIRMATION factor only. Nothing here may map "PCR up" to "buy CE".
"""

from __future__ import annotations

from typing import Optional, Sequence

from models.feature_models import PcrFeatures, TrendState
from models.market_models import OptionChain

QUALITY_OK = "OK"


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    """Put/Call ratio with explicit zero-denominator handling (never crashes)."""
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def compute_pcr(chain: OptionChain, atm_strike: int, atm_range: int) -> PcrFeatures:
    """PCR over the full chain and over the ATM +/- range window.

    Quality:
      OK          — every selected ATM-range strike present, ratios computed
      INCOMPLETE  — some selected strikes missing; ratios computed from what exists
      INVALID     — a required denominator is zero (PCR undefined) or chain empty
    """
    total_put = sum(r.pe.oi for r in chain.rows)
    total_call = sum(r.ce.oi for r in chain.rows)

    if not chain.rows:
        return PcrFeatures(
            total_put_oi=0,
            total_call_oi=0,
            atm_put_oi=0,
            atm_call_oi=0,
            quality="INVALID: empty option chain",
            valid=False,
            reason="option chain has no rows; PCR not computable",
        )

    band = atm_range * chain.strike_interval
    atm_rows = [r for r in chain.rows if abs(r.strike - atm_strike) <= band]
    atm_put = sum(r.pe.oi for r in atm_rows)
    atm_call = sum(r.ce.oi for r in atm_rows)

    total_pcr = _ratio(total_put, total_call)
    atm_pcr = _ratio(atm_put, atm_call)

    if total_call == 0 or total_put == 0 and total_call == 0:
        quality = f"INVALID: total call OI is zero (put={total_put}, call={total_call})"
        valid = False
        reason = "PCR undefined: zero total call OI"
    elif atm_pcr is None:
        quality = f"INVALID: ATM-range call OI is zero (put={atm_put}, call={atm_call})"
        valid = False
        reason = "ATM-range PCR undefined: zero call OI in ATM window"
    else:
        selected = 2 * atm_range + 1
        missing = selected - len(atm_rows)
        if missing > 0:
            quality = f"INCOMPLETE: {missing} of {selected} ATM-range strikes missing"
            valid = True
            reason = "PCR computed from available strikes; ATM window incomplete"
        else:
            quality = QUALITY_OK
            valid = True
            reason = ""

    return PcrFeatures(
        total_put_oi=total_put,
        total_call_oi=total_call,
        total_pcr=total_pcr,
        atm_put_oi=atm_put,
        atm_call_oi=atm_call,
        atm_pcr=atm_pcr,
        pcr_change=None,
        pcr_trend=TrendState.INSUFFICIENT_DATA,  # set by trend_from_history
        quality=quality,
        valid=valid,
        reason=reason,
    )


def pcr_change(current_pcr: Optional[float], previous_pcr: Optional[float]) -> Optional[float]:
    """PCR change vs the previous snapshot (None when either side is unknown)."""
    if current_pcr is None or previous_pcr is None:
        return None
    return round(current_pcr - previous_pcr, 4)


def pcr_trend_from_history(
    history: Sequence[tuple[object, float]],
    lookback: int,
    flat_band: float,
) -> TrendState:
    """Trend over the last `lookback` PCR samples (oldest -> newest, current last).

    Uses the configured lookback window — never a single tick. delta = last-first:
      UP    when delta >  flat_band
      DOWN  when delta < -flat_band
      FLAT  otherwise
      INSUFFICIENT_DATA when fewer than `lookback` usable samples exist
    """
    values = [p for _, p in history if p is not None]
    if len(values) < max(lookback, 2):
        return TrendState.INSUFFICIENT_DATA
    window = values[-lookback:]
    delta = window[-1] - window[0]
    if delta > flat_band:
        return TrendState.UP
    if delta < -flat_band:
        return TrendState.DOWN
    return TrendState.FLAT
