"""Phase C — cooldown (§19/§20/§35).

Boundary semantics (documented): the cooldown window is [T, T+10) — at exactly
T+10 minutes a fresh setup is eligible again. During cooldown, qualifying
setups (same side or opposite side) surface as WAIT / SIGNAL_COOLDOWN.
"""

from datetime import datetime, timedelta

from engines.fixtures import default_config, strong_ce_features, strong_pe_features
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.state_machine import SignalState
from lib.dates import IST

CFG = default_config()
T0 = datetime(2025, 1, 15, 11, 40, 0, tzinfo=IST)  # the 11:40 CE_SETUP from master prompt §19
F = strong_ce_features()
P = strong_pe_features()


def test_emit_then_cooldown_then_fresh_at_exact_boundary():
    mem = SignalMemory()
    d0 = evaluate_and_apply(mem, F, CFG, T0)
    assert d0.decision == "CE_SETUP" and d0.state == "WATCH"
    d1 = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=1))
    assert d1.decision == "WAIT"
    assert any("SIGNAL_COOLDOWN" in r for r in d1.reasons)
    d2 = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=9, seconds=59))
    assert d2.decision == "WAIT"
    assert any("SIGNAL_COOLDOWN" in r for r in d2.reasons)
    d3 = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=10))
    assert d3.decision == "CE_SETUP"  # T+10:00 exactly -> eligible for fresh evaluation
    assert d3.state == "ENTRY_CANDIDATE"  # the fresh emission advanced the ramp


def test_opposite_side_during_cooldown_waits_and_is_logged():
    mem = SignalMemory()
    assert evaluate_and_apply(mem, F, CFG, T0).decision == "CE_SETUP"
    d = evaluate_and_apply(mem, P, CFG, T0 + timedelta(minutes=1))
    assert d.decision == "WAIT"
    assert any("SIGNAL_COOLDOWN" in r for r in d.reasons)
    assert any("opposite-side PE" in r for r in d.reasons)  # §20: logged, not emitted


def test_opposite_side_after_cooldown_still_blocked():
    mem = SignalMemory()
    assert evaluate_and_apply(mem, F, CFG, T0).decision == "CE_SETUP"
    d = evaluate_and_apply(mem, P, CFG, T0 + timedelta(minutes=10))
    assert d.decision == "WAIT"
    assert any("OPPOSITE_SIDE_PENDING" in r for r in d.reasons)


def test_active_signal_does_not_spam_repeats():
    mem = SignalMemory()
    for minutes in (0, 10, 20):  # three emissions ramp WAIT->WATCH->ENTRY_CANDIDATE->ACTIVE
        evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=minutes))
    assert mem.snapshot().state == SignalState.ACTIVE
    d = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=30))
    assert d.decision == "WAIT"
    assert any("SIGNAL_ALREADY_ACTIVE" in r for r in d.reasons)
