"""Phase C — state machine (§16/§17/§36): explicit states, validated
transitions, research ramp, recovery from exit states."""

from datetime import datetime, timedelta

import pytest

from engines.fixtures import (
    ce_below_vwap_features,
    default_config,
    strong_ce_features,
)
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.state_machine import (
    IllegalTransitionError,
    SignalState,
    transition,
)
from lib.dates import IST

CFG = default_config()
T0 = datetime(2025, 1, 15, 11, 40, 0, tzinfo=IST)
F = strong_ce_features()


def _active_memory() -> SignalMemory:
    """Drive a CE signal through the full research ramp to ACTIVE."""
    mem = SignalMemory()
    for minutes in (0, 10, 20):  # emissions at T0, T0+10, T0+20
        evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=minutes))
    return mem


def test_valid_transitions():
    assert transition(SignalState.WAIT, SignalState.WATCH) == SignalState.WATCH
    assert transition(SignalState.WATCH, SignalState.ENTRY_CANDIDATE) == SignalState.ENTRY_CANDIDATE
    assert transition(SignalState.ENTRY_CANDIDATE, SignalState.ACTIVE) == SignalState.ACTIVE
    assert transition(SignalState.ACTIVE, SignalState.INVALIDATED) == SignalState.INVALIDATED


def test_research_exits_reachable_from_active():
    assert transition(SignalState.ACTIVE, SignalState.TARGET) == SignalState.TARGET
    assert transition(SignalState.ACTIVE, SignalState.STOPLOSS) == SignalState.STOPLOSS
    assert transition(SignalState.ACTIVE, SignalState.EXPIRED) == SignalState.EXPIRED


def test_exit_states_recover_to_wait():
    for state in (SignalState.INVALIDATED, SignalState.TARGET, SignalState.STOPLOSS, SignalState.EXPIRED):
        assert transition(state, SignalState.WAIT) == SignalState.WAIT


def test_staying_in_state_is_allowed():
    assert transition(SignalState.WAIT, SignalState.WAIT) == SignalState.WAIT


def test_illegal_transitions_rejected():
    with pytest.raises(IllegalTransitionError):
        transition(SignalState.WAIT, SignalState.ACTIVE)  # no skipping the ramp
    with pytest.raises(IllegalTransitionError):
        transition(SignalState.ACTIVE, SignalState.WATCH)
    with pytest.raises(IllegalTransitionError):
        transition(SignalState.ENTRY_CANDIDATE, SignalState.TARGET)


def test_full_ramp_via_emissions():
    mem = SignalMemory()
    d1 = evaluate_and_apply(mem, F, CFG, T0)
    assert (d1.decision, d1.state) == ("CE_SETUP", "WATCH")
    d2 = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=10))
    assert (d2.decision, d2.state) == ("CE_SETUP", "ENTRY_CANDIDATE")
    d3 = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=20))
    assert (d3.decision, d3.state) == ("CE_SETUP", "ACTIVE")


def test_active_to_invalidated_to_wait_recovery():
    mem = _active_memory()
    d = evaluate_and_apply(mem, ce_below_vwap_features(), CFG, T0 + timedelta(minutes=21))
    assert d.decision == "CE_INVALIDATED"
    assert d.state == "INVALIDATED"
    assert mem.snapshot().state == SignalState.INVALIDATED
    # The next WAIT evaluation recovers the exit state back to WAIT.
    evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=22))
    assert mem.snapshot().state == SignalState.WAIT
    # And a fresh signal can be emitted afterwards (cooldown still holding).
    d2 = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=40))
    assert d2.decision == "CE_SETUP" and d2.state == "WATCH"


def test_memory_recovers_exit_states_on_next_wait_evaluation():
    """A research exit (e.g. EXPIRED, driven by later-phase tooling) recovers
    to WAIT on the next WAIT evaluation."""
    mem = _active_memory()
    mem._state = transition(mem.snapshot().state, SignalState.EXPIRED)  # research exit
    d = evaluate_and_apply(mem, F, CFG, T0 + timedelta(minutes=21))  # cooldown still active
    assert d.decision == "WAIT"
    assert any("SIGNAL_COOLDOWN" in r for r in d.reasons)
    assert mem.snapshot().state == SignalState.WAIT  # recovered on the WAIT evaluation
