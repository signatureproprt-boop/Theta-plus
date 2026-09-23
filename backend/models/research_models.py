"""Phase I — real-data validation models.

Phase I adds VALIDATION only: dataset provenance, data-quality gating, extra
descriptive analytics and a production-readiness checklist. The strategy
(PCR/OI/VWAP/rules/score/state/cooldown) is untouched and unreadable from here.

Data-origin labels are never mixed (§9):
  REAL_HISTORICAL   genuine historical market snapshots
  LIVE_REAL         genuine live broker market data
  REPLAY_SYNTHETIC  fixture data (proves correctness, NOT market performance)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

ORIGIN_REAL_HISTORICAL = "REAL_HISTORICAL"
ORIGIN_LIVE_REAL = "LIVE_REAL"
ORIGIN_REPLAY_SYNTHETIC = "REPLAY_SYNTHETIC"
ORIGIN_LIVE_SIMULATED = "LIVE_SIMULATED"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_PENDING = "PENDING"
STATUS_OPTIONAL = "OPTIONAL"   # present but NOT a production gate (e.g. Telegram)

SCHEMA_VERSION = "1.0"
ANALYTICS_VERSION = "1.0.0"


class DatasetDescriptor(BaseModel):
    """Versioned dataset identity so any report can be reproduced (§33)."""

    model_config = ConfigDict(frozen=True)

    dataset_id: str
    dataset_hash: str
    source: str
    data_origin: str
    date_from: str = ""
    date_to: str = ""
    trading_days: int = 0
    snapshot_count: int = 0
    created_at: datetime
    schema_version: str = SCHEMA_VERSION
    feature_version: str = ""
    strategy_version: str = ""
    note: str = ""


class DataQualityReport(BaseModel):
    """§8 — computed BEFORE any backtest is allowed to be called meaningful."""

    model_config = ConfigDict(frozen=True)

    dataset_id: str
    data_origin: str
    date_from: str = ""
    date_to: str = ""
    trading_days: int = 0
    snapshot_count: int = 0
    expected_snapshots: Optional[int] = None
    missing_snapshots: Optional[int] = None
    duplicate_snapshots: int = 0
    duplicate_timestamps: int = 0
    out_of_order_snapshots: int = 0
    timestamp_gaps: int = 0
    largest_gap_seconds: Optional[float] = None
    missing_strikes: int = 0
    missing_oi: int = 0
    missing_ltp: int = 0
    invalid_values: int = 0
    expiry_coverage: tuple[str, ...] = ()
    strikes_per_snapshot_min: Optional[int] = None
    strikes_per_snapshot_max: Optional[int] = None
    production_quality: bool = False
    failures: tuple[str, ...] = ()
    note: str = ""


class LeakageAudit(BaseModel):
    """§11 — no signal may see anything after its own timestamp."""

    model_config = ConfigDict(frozen=True)

    checked_signals: int = 0
    future_feature_reads: int = 0
    future_outcome_inputs: int = 0
    passed: bool = True
    detail: str = ""


class SessionResetAudit(BaseModel):
    """§12 — each trading day must start from a clean state."""

    model_config = ConfigDict(frozen=True)

    days: tuple[str, ...] = ()
    per_day_signal_counts: tuple[int, ...] = ()
    combined_matches_per_day: bool = True
    vwap_restarts: bool = True
    cooldown_carryover: bool = False
    passed: bool = True
    detail: str = ""


class DeterminismAudit(BaseModel):
    """§32 — same dataset => same hashes, signals and metrics."""

    model_config = ConfigDict(frozen=True)

    dataset_hash_stable: bool = True
    replay_hash_stable: bool = True
    signal_count_stable: bool = True
    metrics_stable: bool = True
    replay_hash: str = ""
    passed: bool = True


class Bucket(BaseModel):
    """A descriptive breakdown row. `n` is always visible (§17)."""

    model_config = ConfigDict(frozen=True)

    label: str
    n: int = 0
    resolved: int = 0
    target: int = 0
    stoploss: int = 0
    invalidated: int = 0
    gross_points: Optional[float] = None
    avg_mfe: Optional[float] = None
    avg_mae: Optional[float] = None
    target_rate_of_resolved: Optional[float] = None
    denominator: str = "resolved outcomes (target + stoploss)"


class PeriodPnl(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    n: int = 0
    gross_points: float = 0.0
    net_points: float = 0.0


class ExtraAnalytics(BaseModel):
    """Phase I descriptive analytics layered on the Phase E metrics engine.

    Nothing here feeds back into the strategy — these are read-only statistics.
    """

    model_config = ConfigDict(frozen=True)

    sample_size_resolved: int = 0
    sample_size_signals: int = 0
    gross_points_total: Optional[float] = None
    net_points_total: Optional[float] = None
    avg_outcome_points: Optional[float] = None
    median_outcome_points: Optional[float] = None
    profit_factor: Optional[float] = None
    profit_factor_definition: str = (
        "sum of positive gross points / absolute sum of negative gross points "
        "over resolved outcomes"
    )
    expectancy_points: Optional[float] = None
    expectancy_definition: str = (
        "mean gross points per resolved outcome (target + stoploss), not a forecast"
    )
    max_drawdown_points: Optional[float] = None
    drawdown_definition: str = (
        "largest peak-to-trough decline of the cumulative gross-points curve, "
        "ordered by signal timestamp"
    )
    avg_signal_duration_seconds: Optional[float] = None
    by_score_bucket: tuple[Bucket, ...] = ()
    by_score_bucket_fine: tuple[Bucket, ...] = ()   # Phase J §11: 35-44..85-100
    by_time_of_day_30m: tuple[Bucket, ...] = ()     # Phase J §11: 30-minute buckets
    by_time_of_day: tuple[Bucket, ...] = ()
    by_side: tuple[Bucket, ...] = ()
    by_pcr_regime: tuple[Bucket, ...] = ()
    by_vwap_regime: tuple[Bucket, ...] = ()
    by_volatility_regime: tuple[Bucket, ...] = ()
    daily_pnl: tuple[PeriodPnl, ...] = ()
    weekly_pnl: tuple[PeriodPnl, ...] = ()
    monthly_pnl: tuple[PeriodPnl, ...] = ()
    analytics_version: str = ANALYTICS_VERSION
    note: str = ""


class SignalEvidence(BaseModel):
    """§25 — one fully traceable record answering "why did this signal occur?"."""

    model_config = ConfigDict(frozen=True)

    signal_id: str
    timestamp: datetime
    decision: str
    state: str
    spot: Optional[float] = None
    vwap: Optional[float] = None
    vwap_distance: Optional[float] = None
    total_pcr: Optional[float] = None
    pcr_trend: str = ""
    put_oi: Optional[int] = None
    call_oi: Optional[int] = None
    atm: Optional[int] = None
    ce_score: int = 0
    pe_score: int = 0
    max_score: int = 100
    reason: str = ""
    rule_results: tuple[dict, ...] = ()
    outcome: str = "NO_OUTCOME"
    exit_reason: str = ""
    gross_points: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    data_health: str = "OK"
    data_origin: str = ORIGIN_REPLAY_SYNTHETIC
    strategy_version: str = ""
    feature_version: str = ""


class RealValidationReport(BaseModel):
    """The Phase I evidence report."""

    model_config = ConfigDict(frozen=True)

    dataset: DatasetDescriptor
    quality: DataQualityReport
    leakage: LeakageAudit
    session_reset: SessionResetAudit
    determinism: DeterminismAudit
    analytics: ExtraAnalytics
    metrics: dict = {}
    evidence: tuple[SignalEvidence, ...] = ()
    replay_hash: str = ""
    data_origin: str = ORIGIN_REAL_HISTORICAL
    notes: tuple[str, ...] = ()


class ChecklistItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: str
    status: str = STATUS_PENDING
    detail: str = ""


class ReadinessReport(BaseModel):
    """§30 — production readiness checklist. PENDING is an honest answer."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    items: tuple[ChecklistItem, ...] = ()
    passed: int = 0
    failed: int = 0
    pending: int = 0
    optional: int = 0
    mandatory_gates: tuple[str, ...] = ()
    mandatory_pending: tuple[str, ...] = ()
    telegram_required: bool = False
    production_ready: bool = False
    live_execution_enabled: bool = False
    live_execution_armed: bool = False
    note: str = ""


class DatasetStatus(BaseModel):
    """What real data is actually configured right now."""

    model_config = ConfigDict(frozen=True)

    real_historical_configured: bool = False
    real_historical_path_present: bool = False
    real_historical_status: str = STATUS_PENDING
    live_real_data_status: str = STATUS_PENDING
    dhan_credentials_present: bool = False
    instrument_map_present: bool = False
    dataset: Optional[DatasetDescriptor] = None
    quality: Optional[DataQualityReport] = None
    note: str = ""
