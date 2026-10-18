"""Phase D — SETTINGS sheet validation (§3/§13/§20).

Sheet values are typed, range-checked and merged through the SAME AppConfig
validators Phase C uses. Invalid settings are rejected loudly; immutable
safety rules can never be overridden from the sheet.
"""

import pytest

from engines.fixtures import default_config
from sheets.settings import SheetSettingsError, parse_sheet_value, settings_from_sheet

CFG = default_config()


def test_valid_settings_applied():
    new = settings_from_sheet(CFG, {
        "MIN_SCORE": "75",
        "ATM_RANGE": "2",
        "PCR_LOOKBACK": "6",
        "SIGNAL_COOLDOWN": "15",
        "TARGET_POINTS": "40",
        "STOPLOSS_POINTS": "25",
        "SIGNAL_START": "11:45",
    })
    assert new.minimum_score == 75
    assert new.atm_range == 2
    assert new.pcr_lookback == 6
    assert new.signal_cooldown_minutes == 15
    assert new.target_points == 40
    assert new.stoploss_points == 25
    assert new.signal_start_time == "11:45"
    # Weights and versions are untouched by sheet edits.
    assert new.weights == CFG.weights
    assert new.strategy_version == CFG.strategy_version


def test_invalid_score_rejected():
    with pytest.raises(SheetSettingsError) as exc:
        settings_from_sheet(CFG, {"MIN_SCORE": "150"})
    assert "rejected" in str(exc.value)


def test_invalid_atm_range_rejected():
    with pytest.raises(SheetSettingsError):
        settings_from_sheet(CFG, {"ATM_RANGE": "0"})


def test_invalid_cooldown_rejected():
    with pytest.raises(SheetSettingsError):
        settings_from_sheet(CFG, {"SIGNAL_COOLDOWN": "-1"})


def test_invalid_lookback_rejected():
    with pytest.raises(SheetSettingsError):
        settings_from_sheet(CFG, {"PCR_LOOKBACK": "1"})  # needs >= 2


def test_non_numeric_value_rejected():
    with pytest.raises(SheetSettingsError):
        settings_from_sheet(CFG, {"MIN_SCORE": "seventy"})


def test_missing_settings_keep_current_config():
    assert settings_from_sheet(CFG, {}) is CFG
    assert settings_from_sheet(CFG, {"MIN_SCORE": ""}) is CFG  # blank = not set


def test_unknown_key_rejected():
    with pytest.raises(SheetSettingsError):
        settings_from_sheet(CFG, {"WEIGHTS_VWAP": "40"})  # weights are not sheet-editable


def test_immutable_keys_cannot_be_overridden():
    # Matching values are tolerated (they are display mirrors)...
    assert settings_from_sheet(CFG, {"INDEX": "NIFTY"}) is CFG
    # ...but any attempt to change them is rejected.
    for key, value in (
        ("INDEX", "BANKNIFTY"),
        ("STRATEGY_VERSION", "9.9.9"),
        ("TIMEZONE", "UTC"),
        ("FEATURE_ENGINE_VERSION", "2.0.0"),
    ):
        with pytest.raises(SheetSettingsError) as exc:
            settings_from_sheet(CFG, {key: value})
        assert "immutable" in str(exc.value)


def test_system_enabled_can_disable_but_not_force_enable():
    disabled = settings_from_sheet(CFG, {"SYSTEM_ENABLED": "FALSE"})
    assert disabled.enabled is False
    with pytest.raises(SheetSettingsError) as exc:
        settings_from_sheet(disabled, {"SYSTEM_ENABLED": "TRUE"})
    assert "force-enabled" in str(exc.value)


def test_typed_parsing():
    assert parse_sheet_value("MIN_SCORE", " 70 ") == 70
    assert parse_sheet_value("SYSTEM_ENABLED", "true") is True
    assert parse_sheet_value("SYSTEM_ENABLED", "NO") is False
    assert parse_sheet_value("SIGNAL_START", " 11:30 ") == "11:30"
