"""Phase D — SETTINGS sheet <-> typed, validated configuration.

Read safety (§13): every value is typed, range-checked and merged into the
FULL existing configuration, then validated by the SAME AppConfig validators
Phase C uses. Invalid settings are rejected loudly (SystemLog WARN) and the
previous valid configuration is kept — no silent defaults, no partial applies.

Immutable safety rules the sheet can NOT override: instrument (NIFTY-only),
rule weights, strategy/config versions, timezone. SYSTEM_ENABLED may only
DISABLE the system (a stricter state), never force-enable signals past the
health gate — the kill switch is honored inside the signal engine.
"""

from __future__ import annotations

from typing import Optional

from lib.config import AppConfig, ConfigError, config_from_mapping
from sheets.google_sheets import redact

# Keys the control-room sheet may change. Everything else is read-only display.
EDITABLE_KEYS = {
    "MARKET_OPEN": "market_open",
    "SIGNAL_START": "signal_start_time",
    "SIGNAL_END": "signal_end_time",
    "ATM_RANGE": "atm_range",
    "PCR_LOOKBACK": "pcr_lookback",
    "MIN_SCORE": "minimum_score",
    "TARGET_POINTS": "target_points",
    "STOPLOSS_POINTS": "stoploss_points",
    "SIGNAL_COOLDOWN": "signal_cooldown_minutes",
    "SYSTEM_ENABLED": "enabled",
}

# Display-only keys (exposed per Phase D §3 but never applied from the sheet).
DISPLAY_ONLY_KEYS = {"INDEX", "STRATEGY_VERSION", "FEATURE_ENGINE_VERSION", "TIMEZONE"}


def _immutable_expected(cfg: AppConfig, key: str) -> str:
    """The value each immutable key MUST have if present in the sheet."""
    return {
        "INDEX": cfg.instrument,
        "STRATEGY_VERSION": cfg.strategy_version,
        "FEATURE_ENGINE_VERSION": cfg.feature_engine_version,
        "TIMEZONE": cfg.timezone,
    }[key]

INT_KEYS = {"ATM_RANGE", "PCR_LOOKBACK", "MIN_SCORE", "TARGET_POINTS", "STOPLOSS_POINTS", "SIGNAL_COOLDOWN"}
BOOL_KEYS = {"SYSTEM_ENABLED"}


class SheetSettingsError(ValueError):
    """Invalid or rejected sheet settings — message is safe to log."""


def parse_sheet_value(key: str, raw: str):
    """Typed parse of one sheet value; raises SheetSettingsError on garbage."""
    try:
        if key in INT_KEYS:
            return int(str(raw).strip())
        if key in BOOL_KEYS:
            return str(raw).strip().upper() in ("TRUE", "1", "YES", "ON")
        return str(raw).strip()
    except ValueError as exc:
        raise SheetSettingsError(f"invalid value for {key}: {redact(str(raw))}") from exc


def settings_from_sheet(current: AppConfig, sheet_values: dict[str, str]) -> AppConfig:
    """Merge validated sheet edits into the current config; return a NEW AppConfig.

    Rejects: unknown edit keys, immutable keys with changed values, any value
    the shared AppConfig validators refuse (e.g. MIN_SCORE=150, ATM_RANGE=0,
    SIGNAL_COOLDOWN=-1). On any rejection the caller keeps `current`."""
    override: dict = {}
    for key, raw in sheet_values.items():
        key = key.strip().upper()
        if key in DISPLAY_ONLY_KEYS:
            if str(raw).strip() and str(raw).strip() != _immutable_expected(current, key):
                raise SheetSettingsError(
                    f"rejected: {key} is an immutable safety setting (sheet value {redact(str(raw))!r})"
                )
            continue
        if not raw:
            continue
        if key not in EDITABLE_KEYS:
            raise SheetSettingsError(f"rejected: {key} is not sheet-editable")
        field = EDITABLE_KEYS[key]
        value = parse_sheet_value(key, raw)
        if key == "SYSTEM_ENABLED" and value is True and current.enabled is False:
            # Re-enabling is an operator decision made in code/config, not a
            # sheet write — sheet can only disable.
            raise SheetSettingsError("rejected: SYSTEM_ENABLED cannot be force-enabled from the sheet")
        override[field] = value

    if not override:
        return current

    merged = current.model_dump() | override
    try:
        return config_from_mapping(merged)
    except ConfigError as exc:
        raise SheetSettingsError(f"rejected invalid settings: {exc}") from exc
