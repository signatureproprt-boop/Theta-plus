"""Phase B — MarketFeatures: the deterministic feature container.

The Phase C rule/score engines consume ONLY this model (never raw payloads,
never live APIs, never the wall clock). Produced by engines/feature_engine.py
from a MarketSnapshot plus optional history/bars.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from models.market_models import DataHealth, OptionQuote


class TrendState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class AtmFeatures(BaseModel):
    """ATM strike, the selected ±range strikes, and the ATM option quotes."""

    model_config = ConfigDict(frozen=True)

    atm_strike: int
    strike_interval: int
    strikes: tuple[int, ...] = ()
    atm_ce: Optional[OptionQuote] = None
    atm_pe: Optional[OptionQuote] = None
    valid: bool = False
    reason: str = ""


class PcrFeatures(BaseModel):
    """Total and ATM-range PCR plus change/trend. None means not computable —
    never guessed. `valid` is the data-quality flag consumed by the health gate."""

    model_config = ConfigDict(frozen=True)

    total_put_oi: int = 0
    total_call_oi: int = 0
    total_pcr: Optional[float] = None
    atm_put_oi: int = 0
    atm_call_oi: int = 0
    atm_pcr: Optional[float] = None
    pcr_change: Optional[float] = None
    pcr_trend: TrendState = TrendState.INSUFFICIENT_DATA
    quality: str = ""  # OK / INCOMPLETE / INVALID detail
    valid: bool = False
    reason: str = ""


class OiFeatures(BaseModel):
    """Raw OI levels and changes vs the previous snapshot, with a deterministic
    interpretation layer. Raw values are never hidden behind a single label."""

    model_config = ConfigDict(frozen=True)

    call_oi: int = 0
    put_oi: int = 0
    call_oi_change: Optional[int] = None
    put_oi_change: Optional[int] = None
    call_interpretation: str = ""
    put_interpretation: str = ""
    valid: bool = False
    reason: str = ""


class VwapFeatures(BaseModel):
    """Session VWAP (single formula for live and replay) and its relation to spot."""

    model_config = ConfigDict(frozen=True)

    vwap: Optional[float] = None
    distance: Optional[float] = None
    distance_percent: Optional[float] = None
    above_vwap: bool = False
    below_vwap: bool = False
    valid: bool = False
    reason: str = ""


class PriceFeatures(BaseModel):
    """Deterministic price structure — existing features only (no RSI/MACD/...)."""

    model_config = ConfigDict(frozen=True)

    short_term_direction: Direction = Direction.FLAT
    recent_high: Optional[float] = None
    recent_low: Optional[float] = None
    structure_state: str = ""  # BREAKOUT / BREAKDOWN / WITHIN_RANGE
    available: bool = False
    reason: str = ""


class MarketFeatures(BaseModel):
    """The full deterministic feature set for one timestamp."""

    model_config = ConfigDict(frozen=True)

    symbol: str = "NIFTY"
    timestamp: datetime
    spot: float
    atm: AtmFeatures
    pcr: PcrFeatures
    oi: OiFeatures
    vwap: VwapFeatures
    price: PriceFeatures
    data_health: DataHealth
    feature_engine_version: str
