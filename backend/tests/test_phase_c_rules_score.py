"""Phase C — rules, scores, weak/conflict decisions, config validation,
purity and determinism (§5/§11/§12/§13/§24/§25/§32/§33/§34)."""

from datetime import datetime

import pytest

from engines.fixtures import (
    conflict_features,
    default_config,
    strong_ce_features,
    strong_pe_features,
    weak_features,
)
from engines.rule_engine import (
    RULE_ATM,
    RULE_OI_CHANGE,
    RULE_OI_SUPPORT,
    RULE_PCR,
    RULE_PRICE,
    RULE_VWAP,
    evaluate_ce_rules,
)
from engines.score_engine import score_rules
from engines.signal_engine import evaluate
from engines.signal_memory import SignalMemory
from engines.state_machine import SignalState
from lib.config import config_from_mapping, ConfigError
from lib.dates import IST
from models.feature_models import TrendState

NOW = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)
CFG = default_config()
FRESH = SignalMemory().snapshot()


def test_strong_ce_setup_with_exact_rule_contributions():
    f = strong_ce_features()
    d = evaluate(f, CFG, FRESH, NOW)
    assert d.decision == "CE_SETUP"
    assert (d.ce_score, d.ce_max_score) == (100, 100)
    assert d.pe_score == 10  # the rejected side keeps its honest low score
    by_id = {r.rule_id: r for r in d.ce_rules}
    assert by_id[RULE_PCR].score_awarded == 20 and by_id[RULE_PCR].max_score == 20
    assert by_id[RULE_VWAP].score_awarded == 25 and by_id[RULE_VWAP].max_score == 25
    assert by_id[RULE_OI_SUPPORT].score_awarded == 20 and by_id[RULE_OI_SUPPORT].max_score == 20
    assert by_id[RULE_OI_CHANGE].score_awarded == 15 and by_id[RULE_OI_CHANGE].max_score == 15
    assert by_id[RULE_ATM].score_awarded == 10 and by_id[RULE_ATM].max_score == 10
    assert by_id[RULE_PRICE].score_awarded == 10 and by_id[RULE_PRICE].max_score == 10
    for r in d.ce_rules:  # every score is explainable (§5)
        assert r.passed and r.available
        assert r.observed_value and r.reason
    assert (d.atm, d.pcr, d.pcr_trend) == (25050, 1.25, "UP")
    assert d.spot == f.spot and d.vwap == f.vwap.vwap


def test_strong_pe_setup_with_exact_rule_contributions():
    d = evaluate(strong_pe_features(), CFG, FRESH, NOW)
    assert d.decision == "PE_SETUP"
    assert (d.pe_score, d.pe_max_score) == (100, 100)
    assert d.ce_score == 10
    by_id = {r.rule_id: r for r in d.pe_rules}
    assert all(by_id[rid].passed and by_id[rid].score_awarded == by_id[rid].max_score
               for rid in (RULE_PCR, RULE_VWAP, RULE_OI_SUPPORT, RULE_OI_CHANGE, RULE_ATM, RULE_PRICE))
    assert d.pcr_trend == "DOWN"


def test_unavailable_rule_scores_zero_without_normalization():
    f = strong_ce_features().model_copy(update={
        "pcr": strong_ce_features().pcr.model_copy(update={"pcr_trend": TrendState.INSUFFICIENT_DATA})
    })
    d = evaluate(f, CFG, FRESH, NOW)
    assert d.decision == "CE_SETUP"  # 80 still clears the 70 minimum
    assert (d.ce_score, d.ce_max_score) == (80, 100)  # 80/100 — NEVER rescaled to 100
    rule = next(r for r in d.ce_rules if r.rule_id == RULE_PCR)
    assert not rule.available and rule.score_awarded == 0
    assert any("no score normalization" in r for r in d.reasons)


def test_weak_confirmation_waits():
    d = evaluate(weak_features(), CFG, FRESH, NOW)
    assert d.decision == "WAIT"
    assert d.ce_score == 45 and d.pe_score == 45
    assert any("minimum score 70" in r for r in d.reasons)
    assert any("OI_SUPPORT" in r for r in d.reasons)  # WAIT explains itself (§15)


def test_conflicting_signals_wait_never_silently_choose():
    cfg = default_config(minimum_score=0)  # threshold lowered; scores stay honest
    d = evaluate(conflict_features(), cfg, FRESH, NOW)
    assert d.decision == "WAIT"
    assert any("CONFLICTING_SIGNALS" in r for r in d.reasons)
    assert d.ce_score == 80 and d.pe_score == 45


def test_conflict_not_reachable_at_default_threshold():
    # With the V1 rule set, mutual >=70 qualification is logically impossible:
    # PCR trend, VWAP side and price direction are strict complements.
    d = evaluate(conflict_features(), CFG, FRESH, NOW)
    assert d.decision == "CE_SETUP"  # only CE qualifies at 70


def test_custom_weights_are_honored():
    cfg = default_config(weights={
        "pcr_trend": 10, "vwap": 30, "oi_support": 20,
        "oi_change": 15, "atm_proximity": 10, "price_confirmation": 15,
    })
    d = evaluate(strong_ce_features(), cfg, FRESH, NOW)
    by_id = {r.rule_id: r for r in d.ce_rules}
    assert by_id[RULE_PCR].max_score == 10
    assert by_id[RULE_VWAP].max_score == 30
    assert by_id[RULE_PRICE].max_score == 15
    assert (d.ce_score, d.ce_max_score) == (100, 100)


def test_score_is_not_probability():
    d = evaluate(strong_ce_features(), CFG, FRESH, NOW)
    keys = set(d.model_dump().keys())
    assert not any(w in keys for w in ("probability", "accuracy", "chance"))
    dump = d.model_dump_json().lower()
    for banned in ("probability of", "% probability", "success probability",
                   "chance of profit", "accuracy"):
        assert banned not in dump


def test_weights_not_summing_to_100_fail_clearly():
    bad = default_config().model_dump() | {"weights": {
        "pcr_trend": 10, "vwap": 25, "oi_support": 20,
        "oi_change": 15, "atm_proximity": 10, "price_confirmation": 10,  # = 90
    }}
    with pytest.raises(ConfigError) as exc:
        config_from_mapping(bad)
    assert "100" in str(exc.value)


def test_invalid_minimum_score_fails_clearly():
    with pytest.raises(ConfigError):
        default_config(minimum_score=150)


def test_invalid_time_format_fails_clearly():
    with pytest.raises(ConfigError):
        default_config(signal_start_time="25:00")


def test_non_nifty_instrument_fails_clearly():
    with pytest.raises(ConfigError):
        default_config(instrument="BANKNIFTY")  # V1 is NIFTY-only


def test_evaluate_is_pure_and_deterministic():
    f = strong_ce_features()
    mem = SignalMemory()
    s1 = mem.snapshot()
    d1 = evaluate(f, CFG, s1, NOW)
    d2 = evaluate(f, CFG, s1, NOW)
    assert d1.model_dump_json() == d2.model_dump_json()  # identical inputs -> identical output
    assert mem.snapshot().state == SignalState.WAIT  # evaluate() never mutates state (§24)


def test_rule_engine_is_stateless():
    f = strong_ce_features()
    r1 = evaluate_ce_rules(f, CFG)
    r2 = evaluate_ce_rules(f, CFG)
    assert r1 == r2
    ce_score, ce_max = score_rules(r1)
    assert (ce_score, ce_max) == (100, 100)
