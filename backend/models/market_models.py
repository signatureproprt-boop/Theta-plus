"""Phase A — raw, normalized market-data models (immutable value objects).

These models are the boundary between data collection and the feature engine:
the Dhan adapter / simulated feed produce payloads, the normalizer turns them
into these frozen models, and nothing downstream mutates them.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthCode(str, Enum):
    OK = "OK"
    STALE = "STALE"
    INVALID = "INVALID"
    MISSING = "MISSING"
    INCOMPLETE = "INCOMPLETE"


class InstrumentTick(BaseModel):
    """Normalized index quote/candle for the underlying (NIFTY)."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    symbol: str = "NIFTY"
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    vwap: Optional[float] = None  # feed-provided VWAP, informational only — the VWAP engine computes its own
    source: str = "sim"


class OptionQuote(BaseModel):
    """One option side (CE or PE) at one strike. Greeks preserved when present."""

    model_config = ConfigDict(frozen=True)

    ltp: float = 0.0
    oi: int = 0
    oi_change: int = 0
    volume: int = 0
    iv: Optional[float] = None
    greeks: dict[str, float] = Field(default_factory=dict)


class OptionChainRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    strike: int
    ce: OptionQuote
    pe: OptionQuote


class OptionChain(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = "NIFTY"
    timestamp: datetime
    expiry: str = ""
    strike_interval: int
    rows: tuple[OptionChainRow, ...] = ()
    source: str = "sim"


class MarketSnapshot(BaseModel):
    """One immutable market observation — the unit later replay consumes."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    timestamp: datetime
    instrument: str = "NIFTY"
    tick: InstrumentTick
    chain: OptionChain
    source: str = "sim"


class DataHealth(BaseModel):
    """Centralized data-quality status. Component -> HealthCode value string."""

    model_config = ConfigDict(frozen=True)

    status: HealthCode = HealthCode.OK
    age_seconds: float = 0.0
    checks: dict[str, str] = Field(default_factory=dict)
    details: tuple[str, ...] = ()
