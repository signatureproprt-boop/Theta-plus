"""Phase C — data-health hard gate (§10): WAIT regardless of score, plus the
kill switch (master prompt §27)."""

from datetime import datetime

from engines.fixtures import (
    chain_incomplete_features,
    default_config,
    invalid_pcr_features,
    invalid_vwap_features,
    stale_features,
    strong_ce_features,
)
from engines.signal_engine import evaluate
from engines.signal_memory import SignalMemory
from lib.dates import IST

NOW = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)
CFG = default_config()
FRESH = SignalMemory().snapshot()


def _gated(features, code: str):
    d = evaluate(features, CFG, FRESH, NOW)
    assert d.decision == "WAIT"
    assert any(code in r for r in d.reasons)
    return d


def test_stale_data_waits_even_when_score_qualifies():
    d = _gated(stale_features(), "DATA_STALE")
    # The fixture's features are untouched strong-CE economics: the hard gate
    # overrides a score that would otherwise qualify.
    assert d.ce_score >= CFG.minimum_score


def test_invalid_pcr_waits():
    d = _gated(invalid_pcr_features(), "PCR_INVALID")
    assert d.pcr is None  # undefined PCR is never manufactured


def test_invalid_vwap_waits():
    _gated(invalid_vwap_features(), "VWAP_INVALID")


def test_incomplete_option_chain_waits():
    _gated(chain_incomplete_features(), "OPTION_CHAIN_INCOMPLETE")


def test_kill_switch_waits():
    cfg = default_config(enabled=False)
    d = evaluate(strong_ce_features(), cfg, FRESH, NOW)
    assert d.decision == "WAIT"
    assert any("SYSTEM_DISABLED" in r for r in d.reasons)
