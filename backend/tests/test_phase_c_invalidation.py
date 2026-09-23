"""Phase C — deterministic invalidation (§21/§37): thresholded rules only,
no vague terms. "Materially" IS a configured number (pcr_reversal_delta)."""

from datetime import datetime, timedelta

from engines.fixtures import (
    ce_below_vwap_features,
    ce_pcr_dropped_features,
    default_config,
    pe_above_vwap_features,
    strong_ce_features,
    strong_pe_features,
)
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.state_machine import SignalState
from lib.dates import IST

CFG = default_config()  # invalidation: vwap_cross=true, pcr_reversal=true, delta=0.15
T0 = datetime(2025, 1, 15, 11, 40, 0, tzinfo=IST)


def _active(side_features) -> SignalMemory:
    mem = SignalMemory()
    for minutes in (0, 10, 20):
        evaluate_and_apply(mem, side_features, CFG, T0 + timedelta(minutes=minutes))
    assert mem.snapshot().state == SignalState.ACTIVE
    return mem


def test_ce_invalidation_on_vwap_cross():
    mem = _active(strong_ce_features())
    d = evaluate_and_apply(mem, ce_below_vwap_features(), CFG, T0 + timedelta(minutes=21))
    assert d.decision == "CE_INVALIDATED"
    assert any("crossed below session VWAP" in r for r in d.reasons)
    assert d.state == "INVALIDATED"
    assert mem.snapshot().state == SignalState.INVALIDATED


def test_pe_invalidation_on_vwap_cross():
    mem = _active(strong_pe_features())
    d = evaluate_and_apply(mem, pe_above_vwap_features(), CFG, T0 + timedelta(minutes=21))
    assert d.decision == "PE_INVALIDATED"
    assert any("crossed above session VWAP" in r for r in d.reasons)
    assert mem.snapshot().state == SignalState.INVALIDATED


def test_ce_invalidation_on_pcr_reversal():
    mem = _active(strong_ce_features())  # signal_pcr = 1.25 at emission
    # PCR falls 0.20 (>= delta 0.15); VWAP still supportive — PCR rule alone fires.
    d = evaluate_and_apply(mem, ce_pcr_dropped_features(1.05), CFG, T0 + timedelta(minutes=21))
    assert d.decision == "CE_INVALIDATED"
    assert any("PCR reversed materially" in r for r in d.reasons)
    assert mem.snapshot().state == SignalState.INVALIDATED


def test_small_pcr_move_does_not_invalidate():
    mem = _active(strong_ce_features())
    # PCR falls 0.12 (< delta 0.15): not "material" by the configured rule.
    d = evaluate_and_apply(mem, ce_pcr_dropped_features(1.13), CFG, T0 + timedelta(minutes=31))
    assert d.decision == "WAIT"
    assert any("SIGNAL_ALREADY_ACTIVE" in r for r in d.reasons)
    assert mem.snapshot().state == SignalState.ACTIVE


def test_invalidation_rules_fully_disabled():
    mem = _active(strong_ce_features())
    cfg = default_config(invalidation={"vwap_cross": False, "pcr_reversal": False})
    d = evaluate_and_apply(mem, ce_below_vwap_features(), cfg, T0 + timedelta(minutes=31))
    assert d.decision == "WAIT"  # qualifying repeat suppressed, NOT invalidated
    assert mem.snapshot().state == SignalState.ACTIVE
