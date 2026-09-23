"""Phase C — every rule returns a RuleResult: every score must be explainable."""

from __future__ import annotations

from typing import Optional, Sequence

from models.feature_models import Direction, TrendState
from models.signal_models import RuleResult

RULE_PCR = "PCR_CONFIRMATION"
RULE_VWAP = "VWAP_CONFIRMATION"
RULE_OI_SUPPORT = "OI_SUPPORT"
RULE_OI_CHANGE = "OI_CHANGE_SUPPORT"
RULE_ATM = "ATM_DATA_VALID"
RULE_PRICE = "PRICE_CONFIRMATION"

RULE_IDS = (RULE_PCR, RULE_VWAP, RULE_OI_SUPPORT, RULE_OI_CHANGE, RULE_ATM, RULE_PRICE)

SIDE_CE = "CE"
SIDE_PE = "PE"


def _result(
    rule_id: str,
    side: str,
    max_score: int,
    available: bool,
    passed: bool,
    observed: str,
    reason: str,
) -> RuleResult:
    """Missing/unsupportive evidence NEVER awards points (no normalization):
    unavailable rules award 0 and are marked available=False, so the reason
    layer can distinguish "checked and failed" from "could not be checked"."""
    return RuleResult(
        rule_id=rule_id,
        side=side,
        passed=bool(available and passed),
        available=available,
        score_awarded=max_score if (available and passed) else 0,
        max_score=max_score,
        observed_value=observed,
        reason=reason,
    )


def evaluate_ce_rules(features, config) -> tuple[RuleResult, ...]:
    """Deterministic CE confirmation rules — each with an explicit definition.

    PCR_CONFIRMATION   : PCR trend UP over the configured lookback window.
                         (One factor among six — never sufficient alone.)
    VWAP_CONFIRMATION  : spot strictly above session VWAP.
    OI_SUPPORT         : ATM-range put OI >= ATM-range call OI (put-writer
                         support at the money). Configurable interpretation,
                         not a prediction.
    OI_CHANGE_SUPPORT  : put OI added >= call OI added vs previous snapshot.
    ATM_DATA_VALID     : chain covers the ATM window and ATM CE/PE quotes exist
                         with positive LTP, within max_data_age_seconds.
    PRICE_CONFIRMATION : short-term direction UP (existing features only).
    """
    w = config.weights
    f = features
    side = SIDE_CE

    pcr = f.pcr
    pcr_available = pcr.valid and pcr.pcr_trend in (TREND_UP, TREND_DOWN, TREND_FLAT)
    yield_pcr = _result(
        RULE_PCR, side, w.pcr_trend,
        pcr_available, pcr.pcr_trend == TREND_UP,
        f"PCR trend {pcr.pcr_trend.value}, total PCR {_fmt(pcr.total_pcr)}",
        ("PCR trend supportive for CE (UP over lookback)" if pcr_available and pcr.pcr_trend == TREND_UP
         else f"PCR trend not supportive for CE ({pcr.pcr_trend.value})" if pcr_available
         else f"PCR unavailable: {pcr.reason or pcr.quality}"),
    )

    vwap = f.vwap
    yield_vwap = _result(
        RULE_VWAP, side, w.vwap,
        vwap.valid and vwap.vwap is not None, vwap.above_vwap,
        f"spot {f.spot:.2f} vs VWAP {_fmt(vwap.vwap)} (distance {_fmt(vwap.distance)})",
        ("Underlying is above session VWAP" if vwap.valid and vwap.above_vwap
         else "Underlying is not above session VWAP" if vwap.valid
         else f"VWAP unavailable: {vwap.reason}"),
    )

    oi_support_available = pcr.valid
    yield_oi = _result(
        RULE_OI_SUPPORT, side, w.oi_support,
        oi_support_available, pcr.atm_put_oi >= pcr.atm_call_oi,
        f"ATM-range put OI {pcr.atm_put_oi:,} vs call OI {pcr.atm_call_oi:,}",
        ("Put OI support at the ATM range (put OI >= call OI)" if oi_support_available and pcr.atm_put_oi >= pcr.atm_call_oi
         else "Call OI exceeds put OI at the ATM range: no put-writer support" if oi_support_available
         else f"OI support unavailable: {pcr.reason or pcr.quality}"),
    )

    oich = f.oi
    oich_available = oich.put_oi_change is not None and oich.call_oi_change is not None
    yield_oich = _result(
        RULE_OI_CHANGE, side, w.oi_change,
        oich_available,
        oich_available and (oich.put_oi_change >= oich.call_oi_change),
        f"put OI change {_fmt_int(oich.put_oi_change)} vs call OI change {_fmt_int(oich.call_oi_change)}",
        ("Put OI added at least as fast as call OI vs previous snapshot" if oich_available and oich.put_oi_change >= oich.call_oi_change
         else "Call OI added faster than put OI vs previous snapshot" if oich_available
         else f"OI change unavailable: {oich.reason}"),
    )

    atm = f.atm
    atm_ok = (
        atm.valid
        and atm.atm_ce is not None and atm.atm_pe is not None
        and atm.atm_ce.ltp > 0 and atm.atm_pe.ltp > 0
    )
    observed_atm = (
        f"ATM {atm.atm_strike}: CE LTP {atm.atm_ce.ltp:.2f} / PE LTP {atm.atm_pe.ltp:.2f}"
        if atm.atm_ce is not None and atm.atm_pe is not None
        else f"ATM {atm.atm_strike}: quotes missing"
    )
    yield_atm = _result(
        RULE_ATM, side, w.atm_proximity, True, atm_ok, observed_atm,
        (f"ATM option data valid ({observed_atm})" if atm_ok else f"ATM/option data invalid: {atm.reason}"),
    )

    price = f.price
    yield_price = _result(
        RULE_PRICE, side, w.price_confirmation,
        price.available, price.short_term_direction == Direction_UP,
        f"short-term direction {price.short_term_direction.value}, structure {price.structure_state}",
        ("Price structure confirms CE (short-term direction UP)" if price.available and price.short_term_direction == Direction_UP
         else f"Price structure not supportive for CE ({price.short_term_direction.value})" if price.available
         else f"Price confirmation unavailable: {price.reason}"),
    )

    return (yield_pcr, yield_vwap, yield_oi, yield_oich, yield_atm, yield_price)


def evaluate_pe_rules(features, config) -> tuple[RuleResult, ...]:
    """Deterministic PE confirmation rules — mirrors of the CE definitions:
    PCR trend DOWN, spot strictly below VWAP, call OI >= put OI at the ATM
    range, call OI added >= put OI added, valid ATM data, short-term DOWN."""
    w = config.weights
    f = features
    side = SIDE_PE

    pcr = f.pcr
    pcr_available = pcr.valid and pcr.pcr_trend in (TREND_UP, TREND_DOWN, TREND_FLAT)
    yield_pcr = _result(
        RULE_PCR, side, w.pcr_trend,
        pcr_available, pcr.pcr_trend == TREND_DOWN,
        f"PCR trend {pcr.pcr_trend.value}, total PCR {_fmt(pcr.total_pcr)}",
        ("PCR trend supportive for PE (DOWN over lookback)" if pcr_available and pcr.pcr_trend == TREND_DOWN
         else f"PCR trend not supportive for PE ({pcr.pcr_trend.value})" if pcr_available
         else f"PCR unavailable: {pcr.reason or pcr.quality}"),
    )

    vwap = f.vwap
    yield_vwap = _result(
        RULE_VWAP, side, w.vwap,
        vwap.valid and vwap.vwap is not None, vwap.below_vwap,
        f"spot {f.spot:.2f} vs VWAP {_fmt(vwap.vwap)} (distance {_fmt(vwap.distance)})",
        ("Underlying is below session VWAP" if vwap.valid and vwap.below_vwap
         else "Underlying is not below session VWAP" if vwap.valid
         else f"VWAP unavailable: {vwap.reason}"),
    )

    oi_support_available = pcr.valid
    yield_oi = _result(
        RULE_OI_SUPPORT, side, w.oi_support,
        oi_support_available, pcr.atm_call_oi >= pcr.atm_put_oi,
        f"ATM-range call OI {pcr.atm_call_oi:,} vs put OI {pcr.atm_put_oi:,}",
        ("Call OI support at the ATM range (call OI >= put OI)" if oi_support_available and pcr.atm_call_oi >= pcr.atm_put_oi
         else "Put OI exceeds call OI at the ATM range: no call-writer resistance" if oi_support_available
         else f"OI support unavailable: {pcr.reason or pcr.quality}"),
    )

    oich = f.oi
    oich_available = oich.put_oi_change is not None and oich.call_oi_change is not None
    yield_oich = _result(
        RULE_OI_CHANGE, side, w.oi_change,
        oich_available,
        oich_available and (oich.call_oi_change >= oich.put_oi_change),
        f"call OI change {_fmt_int(oich.call_oi_change)} vs put OI change {_fmt_int(oich.put_oi_change)}",
        ("Call OI added at least as fast as put OI vs previous snapshot" if oich_available and oich.call_oi_change >= oich.put_oi_change
         else "Put OI added faster than call OI vs previous snapshot" if oich_available
         else f"OI change unavailable: {oich.reason}"),
    )

    atm = f.atm
    atm_ok = (
        atm.valid
        and atm.atm_ce is not None and atm.atm_pe is not None
        and atm.atm_ce.ltp > 0 and atm.atm_pe.ltp > 0
    )
    observed_atm = (
        f"ATM {atm.atm_strike}: CE LTP {atm.atm_ce.ltp:.2f} / PE LTP {atm.atm_pe.ltp:.2f}"
        if atm.atm_ce is not None and atm.atm_pe is not None
        else f"ATM {atm.atm_strike}: quotes missing"
    )
    yield_atm = _result(
        RULE_ATM, side, w.atm_proximity, True, atm_ok, observed_atm,
        (f"ATM option data valid ({observed_atm})" if atm_ok else f"ATM/option data invalid: {atm.reason}"),
    )

    price = f.price
    yield_price = _result(
        RULE_PRICE, side, w.price_confirmation,
        price.available, price.short_term_direction == Direction_DOWN,
        f"short-term direction {price.short_term_direction.value}, structure {price.structure_state}",
        ("Price structure confirms PE (short-term direction DOWN)" if price.available and price.short_term_direction == Direction_DOWN
         else f"Price structure not supportive for PE ({price.short_term_direction.value})" if price.available
         else f"Price confirmation unavailable: {price.reason}"),
    )

    return (yield_pcr, yield_vwap, yield_oi, yield_oich, yield_atm, yield_price)


def _fmt(v: Optional[float]) -> str:
    return f"{v:.2f}" if v is not None else "n/a"


def _fmt_int(v: Optional[int]) -> str:
    return f"{v:+,}" if v is not None else "n/a"


# Trend/Direction shorthands used by the rule definitions above
TREND_UP = TrendState.UP
TREND_DOWN = TrendState.DOWN
TREND_FLAT = TrendState.FLAT
Direction_UP = Direction.UP
Direction_DOWN = Direction.DOWN
