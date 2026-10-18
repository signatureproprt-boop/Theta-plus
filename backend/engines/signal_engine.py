"""Phase C — signal engine: features -> decision. PURE evaluation.

Determinism contract (§24/§25): given identical MarketFeatures, configuration,
state snapshot and timestamp, `evaluate()` returns an identical SignalDecision.
No randomness, no live APIs, no wall-clock reads — `now` is always a parameter.
State mutation lives exclusively in SignalMemory.apply().

Decision order (§13):
  1. kill switch            -> WAIT / SYSTEM_DISABLED
  2. data-health hard gate  -> WAIT / DATA_STALE | PCR_INVALID | VWAP_INVALID | ...
  3. time filter            -> WAIT / TIME_FILTER_BEFORE_START | TIME_FILTER_AFTER_END
  4. invalidation (ACTIVE)  -> CE_INVALIDATED | PE_INVALIDATED
  5. cooldown gate          -> WAIT / SIGNAL_COOLDOWN   (any qualifying side)
  6. conflict               -> WAIT / CONFLICTING_SIGNALS  (both sides qualify)
  7. single-side qualify    -> CE_SETUP | PE_SETUP (or anti-spam WAIT reasons)
  8. otherwise              -> WAIT with a full explanation
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from engines.rule_engine import evaluate_ce_rules, evaluate_pe_rules
from engines.score_engine import explain_side, score_rules
from engines.signal_memory import SignalMemory, SignalStateSnapshot
from engines.state_machine import SignalState
from lib.dates import parse_hhmm, to_ist
from models.market_models import HealthCode
from models.feature_models import MarketFeatures
from models.signal_models import (
    DECISION_CE_INVALIDATED,
    DECISION_CE_SETUP,
    DECISION_PE_INVALIDATED,
    DECISION_PE_SETUP,
    DECISION_WAIT,
    SignalDecision,
)

_REASON_TIME_BEFORE = "TIME_FILTER_BEFORE_START"
_REASON_TIME_AFTER = "TIME_FILTER_AFTER_END"

_HEALTH_REASON_CODES: dict[tuple[str, str], str] = {
    ("DATA_AGE", "STALE"): "DATA_STALE",
    ("DATA_AGE", "INVALID"): "DATA_INVALID",
    ("DATA_AGE", "MISSING"): "DATA_MISSING",
    ("DATA_AGE", "INCOMPLETE"): "DATA_INCOMPLETE",
    ("PCR", "INVALID"): "PCR_INVALID",
    ("PCR", "MISSING"): "PCR_MISSING",
    ("PCR", "INCOMPLETE"): "PCR_INCOMPLETE",
    ("VWAP", "INVALID"): "VWAP_INVALID",
    ("VWAP", "MISSING"): "VWAP_MISSING",
    ("OI", "MISSING"): "OI_MISSING",
    ("OI", "INVALID"): "OI_INVALID",
    ("OPTION_CHAIN", "INCOMPLETE"): "OPTION_CHAIN_INCOMPLETE",
    ("OPTION_CHAIN", "INVALID"): "OPTION_CHAIN_INVALID",
    ("OPTION_CHAIN", "STALE"): "OPTION_CHAIN_STALE",
    ("PRICE", "INVALID"): "PRICE_INVALID",
}


def _health_reasons(features: MarketFeatures) -> list[str]:
    """Deterministic reason codes for every non-OK component check."""
    reasons = [
        _HEALTH_REASON_CODES.get((component, status), f"{component}_{status}")
        for component, status in sorted(features.data_health.checks.items())
        if status != HealthCode.OK.value
    ]
    return reasons or [f"DATA_{features.data_health.status.value}"]


def time_filter_reason(now: datetime, config) -> Optional[str]:
    """None inside the signal window; else the WAIT reason code.

    Boundary behavior (documented + tested): the start time itself is eligible
    (11:29 WAIT, 11:30 active). The end time is eligible when
    signal_end_inclusive (15:15 eligible, 15:16 not); with
    signal_end_inclusive=false, 15:15 is already after the end.
    """
    t = to_ist(now, config.timezone).time()
    if t < parse_hhmm(config.signal_start_time):
        return _REASON_TIME_BEFORE
    end = parse_hhmm(config.signal_end_time)
    after = t > end if config.signal_end_inclusive else t >= end
    if after:
        return _REASON_TIME_AFTER
    return None


def invalidation_reason(features: MarketFeatures, config, state: SignalStateSnapshot) -> Optional[str]:
    """Deterministic invalidation for the ACTIVE signal (§21). No vague terms:
    every trigger is a configured, thresholded rule.

    CE: spot crosses below VWAP (vwap_cross=true), and/or PCR falls from the
        PCR-at-signal by >= invalidation.pcr_reversal_delta.
    PE: mirror (above VWAP; PCR rise >= delta).
    """
    if state.state != SignalState.ACTIVE or not state.current_side:
        return None
    inv = config.invalidation
    side = state.current_side

    if side == "CE":
        if inv.vwap_cross and features.vwap.valid and features.vwap.below_vwap:
            return (
                f"spot crossed below session VWAP "
                f"(spot {features.spot:.2f}, VWAP {features.vwap.vwap:.2f})"
            )
        if inv.pcr_reversal and state.signal_pcr is not None and features.pcr.total_pcr is not None:
            drop = state.signal_pcr - features.pcr.total_pcr
            if drop >= inv.pcr_reversal_delta:
                return (
                    f"PCR reversed materially for CE: {state.signal_pcr:.2f} -> "
                    f"{features.pcr.total_pcr:.2f} (drop {drop:.2f} >= {inv.pcr_reversal_delta:.2f})"
                )
    else:
        if inv.vwap_cross and features.vwap.valid and features.vwap.above_vwap:
            return (
                f"spot crossed above session VWAP "
                f"(spot {features.spot:.2f}, VWAP {features.vwap.vwap:.2f})"
            )
        if inv.pcr_reversal and state.signal_pcr is not None and features.pcr.total_pcr is not None:
            rise = features.pcr.total_pcr - state.signal_pcr
            if rise >= inv.pcr_reversal_delta:
                return (
                    f"PCR reversed materially for PE: {state.signal_pcr:.2f} -> "
                    f"{features.pcr.total_pcr:.2f} (rise {rise:.2f} >= {inv.pcr_reversal_delta:.2f})"
                )
    return None


def evaluate(features: MarketFeatures, config, state: SignalStateSnapshot, now: datetime) -> SignalDecision:
    """Pure evaluation. Returns the decision with `state` = the CURRENT
    (pre-apply) state; use evaluate_and_apply for the post-apply state."""
    now = to_ist(now, config.timezone)

    ce_rules = evaluate_ce_rules(features, config)
    pe_rules = evaluate_pe_rules(features, config)
    ce_score, ce_max = score_rules(ce_rules)
    pe_score, pe_max = score_rules(pe_rules)
    ce_ok = ce_score >= config.minimum_score
    pe_ok = pe_score >= config.minimum_score
    cooldown_active = state.cooldown_until is not None and now < state.cooldown_until

    decision = DECISION_WAIT
    qualifying: Optional[str] = None
    reasons: list[str] = []

    # 1. kill switch
    if not config.enabled:
        reasons = ["SYSTEM_DISABLED: kill switch is off — no signals can be generated"]
    # 2. data-health hard gate: regardless of score
    elif features.data_health.status != HealthCode.OK:
        reasons = [f"{code}: data health gate failed" for code in _health_reasons(features)]
    # 3. time filter
    elif (tf := time_filter_reason(now, config)) is not None:
        if tf == _REASON_TIME_BEFORE:
            reasons = [f"{tf}: before signal start {config.signal_start_time} — data collection may continue, signals do not"]
        else:
            reasons = [f"{tf}: after signal end {config.signal_end_time} — no new signals"]
    # 4. deterministic invalidation of the ACTIVE research signal
    elif (inv := invalidation_reason(features, config, state)) is not None:
        decision = f"{state.current_side}_INVALIDATED"
        reasons = [f"{decision}: {inv}"]
    # 5. cooldown gate (§13 puts it before conflict; §20: opposite side included)
    elif cooldown_active:
        reasons = [
            f"SIGNAL_COOLDOWN: setup emitted at {_iso(state.signal_timestamp)}, "
            f"cooldown until {_iso(state.cooldown_until)}"
        ]
        if ce_ok and pe_ok:
            reasons.append("both CE and PE currently qualify; suppressed by cooldown")
        elif ce_ok or pe_ok:
            side = "CE" if ce_ok else "PE"
            if state.current_side and side != state.current_side:
                reasons.append(
                    f"opposite-side {side} qualification suppressed by cooldown "
                    f"(opposite-side signals require explicit invalidation first)"
                )
            else:
                reasons.append(f"qualifying {side} setup suppressed; repeated identical signals do not spam")
    # 6. conflict: never silently choose
    elif ce_ok and pe_ok:
        reasons = [
            "CONFLICTING_SIGNALS: both CE and PE qualify; never silently choose one",
            *explain_side("CE", ce_rules, ce_score, ce_max, config.minimum_score),
            *explain_side("PE", pe_rules, pe_score, pe_max, config.minimum_score),
        ]
    # 7. single-side qualification
    elif ce_ok or pe_ok:
        side = "CE" if ce_ok else "PE"
        score, max_score, rules = (
            (ce_score, ce_max, ce_rules) if side == "CE" else (pe_score, pe_max, pe_rules)
        )
        if state.state == SignalState.ACTIVE and state.current_side == side:
            reasons = [
                f"SIGNAL_ALREADY_ACTIVE: {side} setup already active as a research signal; "
                f"repeated identical setups are suppressed"
            ]
        elif state.current_side and state.current_side != side and state.state in (
            SignalState.WATCH,
            SignalState.ENTRY_CANDIDATE,
            SignalState.ACTIVE,
        ):
            reasons = [
                f"OPPOSITE_SIDE_PENDING: {side} qualifies but a {state.current_side} "
                f"signal is still pending; it must be invalidated first"
            ]
        else:
            decision = f"{side}_SETUP"
            qualifying = side
            reasons = [
                f"{decision}: score {score}/{max_score} >= minimum {config.minimum_score} "
                f"(score measures configured rule confirmation, NOT probability)",
                *[f"passed {r.rule_id}: {r.reason}" for r in rules if r.passed],
                *[
                    f"unavailable {r.rule_id}: {r.reason} (no points; no score normalization)"
                    for r in rules
                    if not r.available
                ],
            ]
    # 8. weak / insufficient confirmation
    else:
        reasons = [
            f"confirmation insufficient: no side reached the minimum score {config.minimum_score}",
            *explain_side("CE", ce_rules, ce_score, ce_max, config.minimum_score),
            *explain_side("PE", pe_rules, pe_score, pe_max, config.minimum_score),
        ]

    return SignalDecision(
        timestamp=now,
        symbol=features.symbol,
        decision=decision,
        state=state.state.value,
        ce_score=ce_score,
        pe_score=pe_score,
        ce_max_score=ce_max,
        pe_max_score=pe_max,
        qualifying_side=qualifying,
        reasons=tuple(reasons),
        ce_rules=ce_rules,
        pe_rules=pe_rules,
        atm=features.atm.atm_strike,
        spot=features.spot,
        vwap=features.vwap.vwap,
        pcr=features.pcr.total_pcr,
        pcr_trend=features.pcr.pcr_trend.value,
        strategy_version=config.strategy_version,
        feature_engine_version=features.feature_engine_version,
        config_version=config.config_version,
        data_health=features.data_health.status.value,
        cooldown_until=state.cooldown_until,
    )


def evaluate_and_apply(memory: SignalMemory, features: MarketFeatures, config, now: datetime) -> SignalDecision:
    """Pure evaluate + explicit state mutation (kept separate, §24). Returns the
    decision with `state` and `cooldown_until` updated to the post-apply values."""
    decision = evaluate(features, config, memory.snapshot(), now)
    memory.apply(decision, now, config.signal_cooldown_minutes)
    after = memory.snapshot()
    updates: dict = {}
    if after.state.value != decision.state:
        updates["state"] = after.state.value
    if after.cooldown_until != decision.cooldown_until:
        updates["cooldown_until"] = after.cooldown_until
    if updates:
        decision = decision.model_copy(update=updates)
    return decision


def _iso(dt: Optional[datetime]) -> str:
    return dt.astimezone().isoformat() if dt else "n/a"
