"""Phase D — DashboardPayload: THE canonical dashboard mapping.

Built once by sheets/dashboard.py from (MarketFeatures, SignalDecision, runtime
state) and consumed by TWO surfaces that must never diverge:
  * GET /api/dashboard  (web control room)
  * the DASHBOARD tab in Google Sheets (control room sheet)
No strategy logic lives here — pure display mapping only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


class SystemInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    strategy_version: str
    config_version: str
    feature_engine_version: str
    data_source: str  # sim | dhan


class MarketPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: str = "NIFTY"
    spot: Optional[float] = None
    vwap: Optional[float] = None
    vwap_distance: Optional[float] = None
    vwap_distance_percent: Optional[float] = None
    above_vwap: bool = False
    below_vwap: bool = False
    atm: Optional[int] = None
    last_update: Optional[datetime] = None


class PcrPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_pcr: Optional[float] = None
    atm_pcr: Optional[float] = None
    pcr_change: Optional[float] = None
    atm_pcr_change: Optional[float] = None
    pcr_trend: str = "INSUFFICIENT_DATA"
    atm_pcr_trend: str = "INSUFFICIENT_DATA"


class OiPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    put_oi: Optional[int] = None
    call_oi: Optional[int] = None
    put_oi_change: Optional[int] = None
    call_oi_change: Optional[int] = None


class SignalPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: str  # display label: "CE SETUP" / "PE SETUP" / "WAIT" — never subjective labels
    decision_code: str  # CE_SETUP / PE_SETUP / WAIT / *_INVALIDATED
    ce_score: int = 0
    pe_score: int = 0
    ce_max_score: int = 100
    pe_max_score: int = 100
    state: str = "WAIT"
    signal_time: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    reasons: Tuple[str, ...] = ()
    confirmed: Tuple[str, ...] = ()
    unavailable: Tuple[str, ...] = ()


class OptionPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    atm_strike: Optional[int] = None
    atm_ce_ltp: Optional[float] = None
    atm_pe_ltp: Optional[float] = None
    ce_oi: Optional[int] = None
    pe_oi: Optional[int] = None
    ce_oi_change: Optional[int] = None
    pe_oi_change: Optional[int] = None
    ce_iv: Optional[float] = None
    pe_iv: Optional[float] = None


class HealthItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    component: str
    status: str  # OK / WARNING / STALE / ERROR / DISABLED
    detail: str = ""


class DataHealthPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    overall: str = "DISABLED"  # OK / WARNING / STALE / ERROR / DISABLED
    data_age_seconds: Optional[float] = None
    items: Tuple[HealthItem, ...] = ()


class SettingsPanel(BaseModel):
    """Read-only control-room view of the effective, validated settings."""

    model_config = ConfigDict(frozen=True)

    index: str = "NIFTY"
    market_open: str = "09:15"
    signal_start: str = "11:30"
    signal_end: str = "15:15"
    atm_range: int = 3
    pcr_lookback: int = 5
    min_score: int = 70
    target_points: int = 35
    stoploss_points: int = 30
    signal_cooldown: int = 10
    system_enabled: bool = True
    timezone: str = "Asia/Kolkata"
    strategy_version: str = ""
    feature_engine_version: str = ""


class SystemLogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    level: str  # INFO / WARN / ERROR
    component: str
    event: str
    message: str


class SheetsStatusPanel(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    configured: bool = False  # credentials present in env
    last_flush_at: Optional[datetime] = None
    last_error: str = ""
    rows_written: int = 0


class DashboardPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    system: SystemInfo
    market: MarketPanel = Field(default_factory=MarketPanel)
    pcr: PcrPanel = Field(default_factory=PcrPanel)
    oi: OiPanel = Field(default_factory=OiPanel)
    signal: SignalPanel = Field(default_factory=SignalPanel)
    option: OptionPanel = Field(default_factory=OptionPanel)
    data_health: DataHealthPanel = Field(default_factory=DataHealthPanel)
    settings: SettingsPanel = Field(default_factory=SettingsPanel)
    sheets: SheetsStatusPanel = Field(default_factory=SheetsStatusPanel)
    system_log: Tuple[SystemLogEntry, ...] = ()
