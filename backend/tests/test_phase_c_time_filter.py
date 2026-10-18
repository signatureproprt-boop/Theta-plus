"""Phase C — time filter (§9/§28): Asia/Kolkata wall clock, exact boundaries.
Naive datetimes are interpreted as IST (documented convention, tested)."""

from datetime import datetime

from engines.fixtures import default_config, strong_ce_features
from engines.signal_engine import evaluate
from engines.signal_memory import SignalMemory
from lib.dates import IST

F = strong_ce_features()  # 100/100 CE economics — only the clock can stop it
CFG = default_config()
FRESH = SignalMemory().snapshot()


def _eval(hour: int, minute: int, tz=IST):
    return evaluate(F, CFG, FRESH, datetime(2025, 1, 15, hour, minute, 0, tzinfo=tz))


def test_1129_waits():
    d = _eval(11, 29)
    assert d.decision == "WAIT"
    assert any("TIME_FILTER_BEFORE_START" in r for r in d.reasons)


def test_1130_is_eligible():
    assert _eval(11, 30).decision == "CE_SETUP"


def test_1515_inclusive_is_eligible():
    assert _eval(15, 15).decision == "CE_SETUP"


def test_1516_waits():
    d = _eval(15, 16)
    assert d.decision == "WAIT"
    assert any("TIME_FILTER_AFTER_END" in r for r in d.reasons)


def test_exclusive_end_boundary_is_configurable():
    cfg = default_config(signal_end_inclusive=False)
    d = evaluate(F, cfg, FRESH, datetime(2025, 1, 15, 15, 15, 0, tzinfo=IST))
    assert d.decision == "WAIT"
    assert any("TIME_FILTER_AFTER_END" in r for r in d.reasons)


def test_naive_datetimes_treated_as_ist_safely():
    naive_early = evaluate(F, CFG, FRESH, datetime(2025, 1, 15, 11, 29, 0))
    assert naive_early.decision == "WAIT"
    assert any("TIME_FILTER_BEFORE_START" in r for r in naive_early.reasons)
    naive_active = evaluate(F, CFG, FRESH, datetime(2025, 1, 15, 11, 30, 0))
    assert naive_active.decision == "CE_SETUP"
