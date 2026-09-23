"""Configuration loader — `config/settings.yaml` is the single source of truth.

Every strategy parameter is configurable here; nothing may be hardcoded in the
engines. Invalid configuration must FAIL CLEARLY (ConfigError) — a mistyped
weight or time must never silently degrade into wrong signals.

Usage:
    from lib.config import get_config          # cached app singleton
    cfg = get_config()
    from lib.config import config_from_mapping # tests: build custom configs
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from lib.dates import parse_hhmm

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


class ConfigError(ValueError):
    """Raised for any invalid, missing or self-contradictory configuration."""


class WeightsConfig(BaseModel):
    """Rule weights. The six values must sum to exactly 100."""

    model_config = ConfigDict(frozen=True)

    pcr_trend: int
    vwap: int
    oi_support: int
    oi_change: int
    atm_proximity: int
    price_confirmation: int


class InvalidationConfig(BaseModel):
    """Deterministic invalidation switches/thresholds (no vague terms)."""

    model_config = ConfigDict(frozen=True)

    vwap_cross: bool = True
    pcr_reversal: bool = True
    pcr_reversal_delta: float = 0.15


class AppConfig(BaseModel):
    """Frozen, validated strategy configuration."""

    model_config = ConfigDict(frozen=True)

    config_version: str
    strategy_version: str
    feature_engine_version: str

    instrument: str
    enabled: bool

    market_open: str
    market_close: str
    signal_start_time: str
    signal_end_time: str
    signal_end_inclusive: bool = True
    timezone: str = "Asia/Kolkata"

    strike_interval: int
    atm_range: int
    pcr_lookback: int
    max_data_age_seconds: int
    pcr_flat_band: float

    price_lookback: int = 5
    price_flat_band_pct: float = 0.01

    minimum_score: int
    signal_cooldown_minutes: int

    target_points: int
    stoploss_points: int

    weights: WeightsConfig
    invalidation: InvalidationConfig

    # --- Phase D runtime knobs (strategy-neutral) ---
    pipeline_interval_seconds: int = 5
    data_source: str = "sim"  # sim | dhan
    sheets_flush_seconds: int = 30
    sheets_enabled: bool = True

    # --- Phase E replay/backtest knobs (validation only; strategy is frozen) ---
    replay_enabled: bool = False
    replay_include_pre_signal_data: bool = True
    replay_strict_ordering: bool = False  # true: reject out-of-order input
    backtest_brokerage_per_trade: float = 0.0
    backtest_slippage_points: float = 0.0
    backtest_charges: float = 0.0
    backtest_quantity: int = 1
    backtest_time_windows: list[str] = [
        "11:30-12:00", "12:00-13:00", "13:00-14:00", "14:00-15:00", "15:00-15:15",
    ]
    vwap_near_points: float = 10.0  # |spot-VWAP| <= this => "near VWAP" regime

    # --- Phase G alerts + PAPER trading (no broker execution exists) ---
    system_mode: str = "RESEARCH"  # RESEARCH | PAPER (live execution NOT IMPLEMENTED)
    alerts_enabled: bool = True
    alert_wait_alerts: bool = False
    paper_trading_enabled: bool = False
    max_open_paper_positions: int = 1
    paper_quantity: int = 1
    paper_slippage_points: float = 0.0
    paper_costs_per_trade: float = 0.0

    # --- Phase H live execution (DEFAULT OFF + DISARMED; env must also allow) ---
    live_execution_enabled: bool = False
    live_execution_armed: bool = False
    live_quantity: int = 1
    live_order_type: str = "LIMIT"
    live_product_type: str = "INTRADAY"
    live_validity: str = "DAY"
    execution_adapter: str = "mock"  # mock | dhan

    risk_enabled: bool = True
    risk_max_trades_per_day: int = 3
    risk_max_open_positions: int = 1
    risk_max_quantity: int = 1
    risk_max_daily_loss: float = 0.0
    risk_max_slippage_points: float = 0.0

    @field_validator("execution_adapter")
    @classmethod
    def _adapter(cls, v: str) -> str:
        if v not in ("mock", "dhan"):
            raise ValueError(f"execution_adapter must be 'mock' or 'dhan' (got {v!r})")
        return v

    @field_validator("live_order_type")
    @classmethod
    def _live_order_type(cls, v: str) -> str:
        allowed = ("LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_MARKET")
        if v not in allowed:
            raise ValueError(f"live_order_type must be one of {allowed} (got {v!r})")
        return v

    @field_validator("live_product_type")
    @classmethod
    def _live_product_type(cls, v: str) -> str:
        allowed = ("CNC", "INTRADAY", "MARGIN", "MTF", "CO", "BO")
        if v not in allowed:
            raise ValueError(f"live_product_type must be one of {allowed} (got {v!r})")
        return v

    @field_validator("live_validity")
    @classmethod
    def _live_validity(cls, v: str) -> str:
        if v not in ("DAY", "IOC"):
            raise ValueError(f"live_validity must be DAY or IOC (got {v!r})")
        return v

    @field_validator("live_quantity", "risk_max_trades_per_day", "risk_max_open_positions",
                     "risk_max_quantity")
    @classmethod
    def _live_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"expected a positive integer, got {v}")
        return v

    @field_validator("risk_max_daily_loss", "risk_max_slippage_points")
    @classmethod
    def _risk_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"expected a non-negative value, got {v}")
        return v

    @field_validator("system_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("RESEARCH", "PAPER", "LIVE"):
            raise ValueError(
                f"system_mode must be RESEARCH, PAPER or LIVE (got {v!r}); LIVE additionally "
                "requires live_execution_enabled + live_execution_armed and the server-side "
                "env switches"
            )
        return v

    @field_validator("max_open_paper_positions", "paper_quantity")
    @classmethod
    def _paper_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"expected a positive integer, got {v}")
        return v

    @field_validator("paper_slippage_points", "paper_costs_per_trade")
    @classmethod
    def _paper_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"expected a non-negative value, got {v}")
        return v

    @field_validator("market_open", "market_close", "signal_start_time", "signal_end_time")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        parse_hhmm(v)  # raises ValueError on garbage
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # unknown zone
            raise ValueError(f"unknown timezone {v!r}: {exc}") from exc
        return v

    @field_validator("instrument")
    @classmethod
    def _nifty_only(cls, v: str) -> str:
        if v != "NIFTY":
            raise ValueError(f"instrument {v!r} is not enabled: V1 supports NIFTY only")
        return v

    @field_validator("minimum_score")
    @classmethod
    def _score_bounds(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"minimum_score must be within 0..100 (got {v})")
        return v

    @field_validator("strike_interval", "atm_range", "target_points", "stoploss_points")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"expected a positive integer, got {v}")
        return v

    @field_validator("pcr_lookback", "price_lookback")
    @classmethod
    def _lookback(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"lookback needs at least 2 samples (got {v})")
        return v

    @field_validator("max_data_age_seconds")
    @classmethod
    def _age(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_data_age_seconds must be >= 1 (got {v})")
        return v

    @field_validator("signal_cooldown_minutes")
    @classmethod
    def _cooldown(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"signal_cooldown_minutes must be >= 0 (got {v})")
        return v

    @field_validator("pcr_flat_band", "price_flat_band_pct")
    @classmethod
    def _band(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"flat band must be >= 0 (got {v})")
        return v

    @field_validator("pipeline_interval_seconds")
    @classmethod
    def _interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"pipeline_interval_seconds must be >= 1 (got {v})")
        return v

    @field_validator("sheets_flush_seconds")
    @classmethod
    def _flush(cls, v: int) -> int:
        if v < 5:
            raise ValueError(f"sheets_flush_seconds must be >= 5 to respect API rate limits (got {v})")
        return v

    @field_validator("data_source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v not in ("sim", "dhan"):
            raise ValueError(f"data_source must be 'sim' or 'dhan' (got {v!r})")
        return v


def config_from_mapping(data: dict) -> AppConfig:
    """Validate a raw mapping into AppConfig; every failure raises ConfigError."""
    try:
        cfg = AppConfig(**data)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{exc}") from exc
    total = (
        cfg.weights.pcr_trend
        + cfg.weights.vwap
        + cfg.weights.oi_support
        + cfg.weights.oi_change
        + cfg.weights.atm_proximity
        + cfg.weights.price_confirmation
    )
    if total != 100:
        raise ConfigError(
            f"weights must sum to exactly 100 (got {total}: "
            f"pcr_trend={cfg.weights.pcr_trend}, vwap={cfg.weights.vwap}, "
            f"oi_support={cfg.weights.oi_support}, oi_change={cfg.weights.oi_change}, "
            f"atm_proximity={cfg.weights.atm_proximity}, price_confirmation={cfg.weights.price_confirmation})"
        )
    return cfg


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate settings.yaml. Missing file/corrupt YAML => ConfigError."""
    p = path or CONFIG_PATH
    if not p.exists():
        raise ConfigError(f"configuration file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p} must contain a YAML mapping of settings")
    return config_from_mapping(data)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Cached app-wide config (routers/harness). Tests build their own via config_from_mapping."""
    return load_config()


def reset_config_cache() -> None:
    get_config.cache_clear()
