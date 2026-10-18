"""Phase F — chart visualization models.

VISUALIZATION ONLY. TradingView is an embedded chart widget; it has NO strategy
authority. Every value in these models is produced by the backend engines
(Phase A-E) and is simply serialized for display:

    TradingView VWAP = visual reference only
    Backend  VWAP    = strategy-authoritative value (the `vwap` field here)

No execution field exists (no order, quantity, buy/sell or broker id).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

# Data origins. "LIVE" = the live pipeline; in this environment that pipeline is
# fed by the SIMULATED feed, so the label says so explicitly (never "LIVE MARKET"
# unless genuine Dhan market data is connected).
ORIGIN_LIVE = "LIVE"
ORIGIN_REPLAY_SYNTHETIC = "REPLAY-SYNTHETIC"

MARKER_SETUP = "SETUP"
MARKER_INVALIDATION = "INVALIDATION"
MARKER_WAIT = "WAIT"

# Markers rendered by default (§5): WAIT is hidden unless explicitly requested.
DEFAULT_VISIBLE_DECISIONS = ("CE_SETUP", "PE_SETUP", "CE_INVALIDATED", "PE_INVALIDATED")


class ChartSignalMarker(BaseModel):
    """One backend-generated marker for the chart. Immutable and auditable."""

    model_config = ConfigDict(frozen=True)

    signal_id: str
    timestamp: datetime  # canonical backend timestamp, always Asia/Kolkata aware
    symbol: str = "NIFTY"

    decision: str  # CE_SETUP | PE_SETUP | CE_INVALIDATED | PE_INVALIDATED | WAIT
    state: str
    marker_kind: str  # SETUP | INVALIDATION | WAIT
    side: Optional[str] = None  # CE | PE

    spot: Optional[float] = None
    vwap: Optional[float] = None  # BACKEND VWAP — the strategy-authoritative value
    vwap_distance: Optional[float] = None

    pcr: Optional[float] = None
    atm_pcr: Optional[float] = None
    pcr_trend: Optional[str] = None

    atm: Optional[int] = None
    ce_oi: Optional[int] = None
    ce_oi_change: Optional[int] = None
    pe_oi: Optional[int] = None
    pe_oi_change: Optional[int] = None

    score: int = 0  # the qualifying side's score for setups; max(ce, pe) otherwise
    ce_score: int = 0
    pe_score: int = 0
    max_score: int = 100  # scores are "X/100" — NEVER a probability or percentage

    reason: str = ""
    data_health: str = "OK"
    stale: bool = False

    strategy_version: str = ""
    feature_version: str = ""
    config_version: str = ""

    data_origin: str = ORIGIN_LIVE
    data_origin_label: str = ""

    # Invalidation events keep the original signal identity (§4): the historical
    # setup marker is never deleted or mutated.
    origin_signal_id: Optional[str] = None
    invalidation_timestamp: Optional[datetime] = None
    invalidation_reason: Optional[str] = None


class ChartSignalsResponse(BaseModel):
    """Payload for GET /api/chart/signals — read-only visualization data."""

    model_config = ConfigDict(frozen=True)

    symbol: str = "NIFTY"
    tradingview_symbol: str = "NSE:NIFTY"
    timeframe: str = "5"
    timezone: str = "Asia/Kolkata"

    data_origin: str = ORIGIN_LIVE
    data_origin_label: str = ""
    include_wait: bool = False

    generated_at: datetime
    count: int = 0
    markers: tuple[ChartSignalMarker, ...] = ()

    # Latest backend context for the "Latest Signal" strip.
    latest_decision: str = "WAIT"
    latest_state: str = "WAIT"
    spot: Optional[float] = None
    vwap: Optional[float] = None  # backend VWAP
    vwap_distance: Optional[float] = None
    data_health: str = "OK"
    stale: bool = False

    notes: tuple[str, ...] = ()
