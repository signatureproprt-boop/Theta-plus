"""Phase E — replay/backtest models. Research validation only; no execution."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

BACKTEST_METHODOLOGY_VERSION = "1.0.0"

# Outcome states (§14) — an exit is NEVER invented when future data is missing.
OUTCOME_TARGET = "TARGET"
OUTCOME_STOPLOSS = "STOPLOSS"
OUTCOME_INVALIDATED = "INVALIDATED"
OUTCOME_EXPIRED = "EXPIRED"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_NO_DATA = "NO_DATA"


class ReplaySignal(BaseModel):
    """One replayed evaluation with its full audit trail (§31)."""

    model_config = ConfigDict(frozen=True)

    signal_id: str
    snapshot_id: str
    timestamp: datetime
    instrument: str
    spot: Optional[float] = None
    vwap: Optional[float] = None
    total_pcr: Optional[float] = None
    pcr_trend: str = ""
    put_oi: Optional[int] = None
    call_oi: Optional[int] = None
    ce_score: int = 0
    pe_score: int = 0
    ce_max_score: int = 100
    pe_max_score: int = 100
    decision: str = "WAIT"
    state: str = "WAIT"
    reason: str = ""
    atm: Optional[int] = None
    above_vwap: bool = False
    below_vwap: bool = False
    data_health: str = "OK"
    strategy_version: str = ""
    feature_engine_version: str = ""
    config_version: str = ""
    rule_results: tuple[dict, ...] = ()  # full RuleResult dumps, both sides


class SignalOutcome(BaseModel):
    """Research outcome for one emitted setup (§11-§16). NOT a trade."""

    model_config = ConfigDict(frozen=True)

    signal_id: str
    timestamp: datetime
    side: str  # CE | PE
    strike: Optional[int] = None
    entry: Optional[float] = None
    target: Optional[float] = None
    stoploss: Optional[float] = None
    exit_price: Optional[float] = None
    exit_state: str = OUTCOME_NO_DATA
    exit_timestamp: Optional[datetime] = None
    mfe: Optional[float] = None  # max(option_ltp - entry)
    mae: Optional[float] = None  # min(option_ltp - entry)
    gross_points: Optional[float] = None
    observations_used: int = 0
    time_to_target_seconds: Optional[float] = None
    time_to_stop_seconds: Optional[float] = None
    time_to_invalidation_seconds: Optional[float] = None
    methodology_version: str = BACKTEST_METHODOLOGY_VERSION
    note: str = ""


class SegmentStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    signals: int = 0
    ce_signals: int = 0
    pe_signals: int = 0
    target: int = 0
    stoploss: int = 0
    ambiguous: int = 0
    invalidated: int = 0
    expired: int = 0
    no_data: int = 0
    avg_mfe: Optional[float] = None
    avg_mae: Optional[float] = None


class DataQualityStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_snapshots: int = 0
    valid_snapshots: int = 0
    invalid_snapshots: int = 0
    stale_snapshots: int = 0
    missing_option_data: int = 0
    missing_vwap_data: int = 0
    signals_suppressed_by_data: int = 0


class CostAssumptions(BaseModel):
    """Research assumptions only — zeros mean the result is HYPOTHETICAL."""

    model_config = ConfigDict(frozen=True)

    brokerage_per_trade: float = 0.0
    slippage_points: float = 0.0
    charges: float = 0.0
    quantity: int = 1
    label: str = "HYPOTHETICAL: no brokerage/slippage/charges assumed"


class ReplayMetrics(BaseModel):
    """Descriptive statistics with explicit denominators (§19). No 'accuracy'."""

    model_config = ConfigDict(frozen=True)

    total_evaluations: int = 0
    total_signals: int = 0
    ce_signals: int = 0
    pe_signals: int = 0
    wait_decisions: int = 0
    invalidation_decisions: int = 0

    target: int = 0
    stoploss: int = 0
    invalidated: int = 0
    expired: int = 0
    ambiguous: int = 0
    no_data: int = 0

    avg_mfe: Optional[float] = None
    avg_mae: Optional[float] = None
    median_mfe: Optional[float] = None
    median_mae: Optional[float] = None
    avg_time_to_target_seconds: Optional[float] = None
    avg_time_to_stop_seconds: Optional[float] = None

    gross_points_total: Optional[float] = None
    resolved_outcomes: int = 0  # denominator for target_rate/stop_rate
    target_rate_of_resolved: Optional[float] = None
    stop_rate_of_resolved: Optional[float] = None
    rate_definition: str = (
        "target_rate_of_resolved = TARGET / (TARGET + STOPLOSS); "
        "AMBIGUOUS, NO_DATA, INVALIDATED and EXPIRED are excluded from this denominator. "
        "This is NOT an accuracy or probability claim."
    )

    by_side: tuple[SegmentStats, ...] = ()
    by_time_window: tuple[SegmentStats, ...] = ()
    by_pcr_regime: tuple[SegmentStats, ...] = ()
    by_vwap_regime: tuple[SegmentStats, ...] = ()
    data_quality: DataQualityStats = Field(default_factory=DataQualityStats)
    costs: CostAssumptions = Field(default_factory=CostAssumptions)

    strategy_version: str = ""
    feature_engine_version: str = ""
    methodology_version: str = BACKTEST_METHODOLOGY_VERSION
    data_origin: str = "SYNTHETIC"  # SYNTHETIC | HISTORICAL — never conflated


class ReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    date_from: str
    date_to: str
    instrument: str = "NIFTY"
    signals: tuple[ReplaySignal, ...] = ()
    outcomes: tuple[SignalOutcome, ...] = ()
    metrics: ReplayMetrics = Field(default_factory=ReplayMetrics)
    replay_hash: str = ""
    ordering: str = "sorted"  # sorted | rejected
    notes: tuple[str, ...] = ()
