"""Phase D — dashboard mapping (§4/§5/§6/§17/§18/§20).

Every feature/decision field must land in the correct dashboard field, with
strict display rules: CE SETUP / PE SETUP / WAIT labels, X/100 scores (never
%), health statuses from OK/WARNING/STALE/ERROR/DISABLED.
"""

from datetime import datetime

from engines.fixtures import (
    default_config,
    stale_features,
    strong_ce_features,
    strong_pe_features,
    weak_features,
)
from engines.signal_engine import evaluate
from engines.signal_memory import SignalMemory
from lib.dates import IST
from models.dashboard_models import SheetsStatusPanel
from sheets.dashboard import build_dashboard, dashboard_grid

NOW = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)
CFG = default_config()
FRESH = SignalMemory().snapshot()


def _payload(features, config=CFG, data_source="sim"):
    decision = evaluate(features, config, FRESH, NOW)
    return build_dashboard(
        features=features, decision=decision, config=config, data_source=data_source,
        memory=FRESH, sheets_status=SheetsStatusPanel(), log_entries=[], generated_at=NOW,
        atm_pcr_change=0.01, atm_pcr_trend="UP",
    )


def test_market_pcr_oi_option_fields_map_correctly():
    f = strong_ce_features()
    p = _payload(f)
    assert p.market.index == "NIFTY"
    assert p.market.spot == f.spot
    assert p.market.vwap == f.vwap.vwap
    assert p.market.vwap_distance == f.vwap.distance
    assert p.market.above_vwap is True and p.market.below_vwap is False
    assert p.market.atm == f.atm.atm_strike
    assert p.market.last_update == f.timestamp

    assert p.pcr.total_pcr == f.pcr.total_pcr
    assert p.pcr.atm_pcr == f.pcr.atm_pcr
    assert p.pcr.pcr_change == f.pcr.pcr_change
    assert p.pcr.pcr_trend == "UP"
    assert p.pcr.atm_pcr_trend == "UP" and p.pcr.atm_pcr_change == 0.01

    assert p.oi.put_oi == f.oi.put_oi and p.oi.call_oi == f.oi.call_oi
    assert p.oi.put_oi_change == f.oi.put_oi_change
    assert p.oi.call_oi_change == f.oi.call_oi_change

    assert p.option.atm_strike == f.atm.atm_strike
    assert p.option.atm_ce_ltp == f.atm.atm_ce.ltp
    assert p.option.atm_pe_ltp == f.atm.atm_pe.ltp


def test_signal_mapping_ce_setup():
    p = _payload(strong_ce_features())
    assert p.signal.decision == "CE SETUP"  # display label
    assert p.signal.decision_code == "CE_SETUP"
    assert (p.signal.ce_score, p.signal.ce_max_score) == (100, 100)
    assert "Spot above VWAP" in p.signal.confirmed
    assert "PCR confirmation" in p.signal.confirmed
    assert "Put OI support" in p.signal.confirmed


def test_signal_mapping_pe_setup():
    p = _payload(strong_pe_features())
    assert p.signal.decision == "PE SETUP"
    assert p.signal.decision_code == "PE_SETUP"
    assert (p.signal.pe_score, p.signal.pe_max_score) == (100, 100)
    assert "Spot below VWAP" in p.signal.confirmed
    assert "Call OI support" in p.signal.confirmed


def test_signal_mapping_wait_has_reason():
    p = _payload(weak_features())
    assert p.signal.decision == "WAIT"
    assert p.signal.decision_code == "WAIT"
    assert p.signal.reasons  # WAIT always explains itself (§6)


def test_unavailable_rules_surface_on_dashboard():
    from models.feature_models import TrendState

    f = strong_ce_features()
    f = f.model_copy(update={"pcr": f.pcr.model_copy(update={"pcr_trend": TrendState.INSUFFICIENT_DATA})})
    p = _payload(f)
    assert "PCR confirmation" in p.signal.unavailable
    assert p.signal.ce_score == 80 and p.signal.ce_max_score == 100  # no normalization


def test_stale_data_dashboard_status_and_wait():
    p = _payload(stale_features())
    assert p.data_health.overall == "STALE"
    assert p.signal.decision == "WAIT"  # stale never hidden behind an old signal (§17)
    assert any("DATA_STALE" in r for r in p.signal.reasons)


def test_invalid_data_maps_to_error_status():
    from engines.fixtures import invalid_pcr_features

    p = _payload(invalid_pcr_features())
    assert p.data_health.overall == "ERROR"
    assert p.signal.decision == "WAIT"


def test_sim_feed_marks_dhan_and_websocket_disabled():
    p = _payload(strong_ce_features(), data_source="sim")
    statuses = {i.component: i.status for i in p.data_health.items}
    assert statuses["DHAN API"] == "DISABLED"
    assert statuses["WEBSOCKET"] == "DISABLED"
    assert statuses["OPTION CHAIN"] == "OK" and statuses["VWAP"] == "OK"


def test_kill_switch_shows_disabled_and_wait():
    cfg = default_config(enabled=False)
    p = _payload(strong_ce_features(), config=cfg)
    assert p.data_health.overall == "DISABLED"
    assert p.system.enabled is False
    assert p.signal.decision == "WAIT"
    assert any("SYSTEM_DISABLED" in r for r in p.signal.reasons)


def test_settings_panel_mirrors_config():
    p = _payload(strong_ce_features())
    s = p.settings
    assert (s.index, s.market_open, s.signal_start, s.signal_end) == ("NIFTY", "09:15", "11:30", "15:15")
    assert (s.atm_range, s.pcr_lookback, s.min_score) == (3, 5, 70)
    assert (s.target_points, s.stoploss_points, s.signal_cooldown) == (35, 30, 10)
    assert s.timezone == "Asia/Kolkata" and s.system_enabled is True
    assert s.strategy_version == CFG.strategy_version


def test_versions_present_on_payload():
    p = _payload(strong_ce_features())
    assert p.system.strategy_version == "1.0.0"
    assert p.system.feature_engine_version == "1.0.0"
    assert p.system.config_version == "1.0"


def test_dashboard_grid_mirrors_payload_for_sheets():
    p = _payload(strong_ce_features())
    grid = dashboard_grid(p)
    keys = {row[0] for row in grid}
    for required in (
        "PCR TRADING SYSTEM", "SYSTEM", "INDEX", "SPOT", "VWAP", "VWAP DISTANCE", "ATM",
        "LAST UPDATE", "TOTAL PCR", "ATM PCR", "PCR CHANGE", "PCR TREND", "ATM PCR TREND",
        "PUT OI", "CALL OI", "PUT OI CHANGE", "CALL OI CHANGE", "CE SCORE", "PE SCORE",
        "MAX CE SCORE", "MAX PE SCORE", "FINAL SIGNAL", "CURRENT STATE", "SIGNAL TIME",
        "COOLDOWN UNTIL", "ATM CE LTP", "ATM PE LTP", "DHAN API", "WEBSOCKET",
        "OPTION CHAIN", "DATA AGE", "OVERALL STATUS", "REASON",
    ):
        assert required in keys, f"DASHBOARD sheet missing row: {required}"
    values = {row[0]: row[1] for row in grid}
    assert values["FINAL SIGNAL"] == "CE SETUP"
    assert values["CE SCORE"] == 100
    assert values["SYSTEM"] == "ENABLED"


def test_no_probability_language_anywhere_on_dashboard():
    dump = _payload(strong_ce_features()).model_dump_json().lower()
    # Banned: any probability/accuracy CLAIM or subjective label (§5).
    for banned in ("% probability", "success probability", "probability of",
                   "chance of profit", "accuracy", "guaranteed",
                   "strong buy", "very bullish", "high probability"):
        assert banned not in dump, f"banned display token leaked: {banned}"
    # Scores are only ever rendered as X/max — never a percentage.
    p = _payload(strong_ce_features())
    assert f"{p.signal.ce_score}/{p.signal.ce_max_score}" == "100/100"
    assert "%" not in p.signal.decision
    # The only allowed use of the word "probability" is the engine's explicit
    # disclaimer that the score is NOT one.
    for reason in p.signal.reasons:
        if "probability" in reason.lower():
            assert "not probability" in reason.lower()
