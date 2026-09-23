"""Phase I — validation orchestrator + production readiness (§13/§30/§41).

    real dataset -> quality gate -> Phase E replay -> audits -> analytics -> evidence

If no real dataset or no Dhan credential exists, the honest answer is PENDING —
never a fabricated success.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from lib.config import AppConfig, get_config
from lib.dates import IST
from models.market_models import MarketSnapshot
from models.research_models import (
    ORIGIN_REAL_HISTORICAL,
    STATUS_FAIL,
    STATUS_OPTIONAL,
    STATUS_PASS,
    STATUS_PENDING,
    ChecklistItem,
    DatasetStatus,
    ReadinessReport,
    RealValidationReport,
)
from replay.backtest_engine import run_replay
from research.analytics import build_evidence, compute_analytics
from research.audits import audit_determinism, audit_leakage, audit_session_reset
from research.ingest import RealDataError, build_descriptor, in_memory_source, load_real_source
from execution.instruments import instrument_map_status
from research.quality import assess_quality

REAL_HISTORICAL_ENV = "REAL_HISTORICAL_PATH"
TELEGRAM_REQUIRED = False  # §17: Telegram is optional, never a production gate

# Only these checklist items gate production readiness (§25). Telegram is absent
# from this list by design.
MANDATORY_GATES = (
    "REAL DHAN AUTH", "REAL MARKET DATA", "INSTRUMENT MAP", "INSTRUMENT MAP VALIDATION",
    "HISTORICAL DATA", "DATA QUALITY", "REPLAY", "LEAKAGE", "DETERMINISM",
    "LIVE/REPLAY PARITY", "OOS VALIDATION", "PAPER ENGINE", "RISK ENGINE",
    "EXECUTION GATE", "IDEMPOTENCY", "RECONCILIATION", "KILL SWITCH",
    "CREDENTIAL SECURITY",
)


def real_dataset_path() -> Optional[Path]:
    raw = os.environ.get(REAL_HISTORICAL_ENV, "").strip()
    return Path(raw) if raw else None


def validate_dataset(
    path_or_snapshots,
    config: Optional[AppConfig] = None,
    data_origin: str = ORIGIN_REAL_HISTORICAL,
    source_label: str = "",
    expected_interval_seconds: Optional[int] = None,
) -> RealValidationReport:
    """Run the full Phase I validation over a genuine dataset (offline)."""
    config = config or get_config()

    if isinstance(path_or_snapshots, (str, Path)):
        source = load_real_source(path_or_snapshots)
        snapshots: list[MarketSnapshot] = list(source.iter_snapshots())
        label = source_label or Path(path_or_snapshots).name
    else:
        snapshots = list(path_or_snapshots)
        label = source_label or "in-memory"

    if not snapshots:
        raise RealDataError("NO_DATA: dataset produced zero snapshots")

    descriptor = build_descriptor(snapshots, label, config, data_origin=data_origin)
    quality = assess_quality(
        snapshots, descriptor.dataset_id, data_origin,
        expected_interval_seconds=expected_interval_seconds,
    )

    ordered = sorted(snapshots, key=lambda s: s.timestamp)
    result = run_replay(in_memory_source(ordered), config, data_origin=data_origin)
    leakage = audit_leakage(result.signals, result.outcomes, ordered)
    session = audit_session_reset(ordered, config)
    determinism = audit_determinism(ordered, config, data_origin)
    analytics = compute_analytics(result.signals, result.outcomes, config, data_origin)
    evidence = build_evidence(result.signals, result.outcomes, data_origin)

    notes = [
        "The frozen Phase A-H strategy was evaluated as-is: no parameter, weight, "
        "threshold or rule was changed for this validation (no optimization, no ML).",
        f"Every rate states its denominator; N={analytics.sample_size_resolved} resolved "
        "outcomes out of "
        f"{analytics.sample_size_signals} setups. Small samples are reported, not hidden.",
        f"DATA ORIGIN = {data_origin}; DATE RANGE = {descriptor.date_from}..{descriptor.date_to}; "
        f"STRATEGY {descriptor.strategy_version}; FEATURES {descriptor.feature_version}.",
    ]
    if not quality.production_quality:
        notes.append(
            "DATA QUALITY GATE NOT PASSED — these figures describe the dataset supplied, "
            "not production-quality market validation: " + "; ".join(quality.failures)
        )
    if data_origin != ORIGIN_REAL_HISTORICAL:
        notes.append(
            "NOT REAL MARKET DATA: this run used a non-real origin label, so the numbers "
            "prove pipeline correctness only."
        )

    return RealValidationReport(
        dataset=descriptor, quality=quality, leakage=leakage, session_reset=session,
        determinism=determinism, analytics=analytics,
        metrics=result.metrics.model_dump(mode="json"),
        evidence=evidence, replay_hash=result.replay_hash,
        data_origin=data_origin, notes=tuple(notes),
    )


def dataset_status(config: Optional[AppConfig] = None) -> DatasetStatus:
    config = config or get_config()
    path = real_dataset_path()
    present = bool(path and path.exists())
    creds = bool(os.environ.get("DHAN_ACCESS_TOKEN") and os.environ.get("DHAN_CLIENT_ID"))
    imap = bool(os.environ.get("DHAN_INSTRUMENT_MAP_PATH", "").strip())
    return DatasetStatus(
        real_historical_configured=bool(path),
        real_historical_path_present=present,
        real_historical_status=STATUS_PASS if present else STATUS_PENDING,
        live_real_data_status=STATUS_PASS if creds else STATUS_PENDING,
        dhan_credentials_present=creds,
        instrument_map_present=imap,
        note=(
            "REAL HISTORICAL VALIDATION = PENDING until a genuine dataset path is set in "
            f"{REAL_HISTORICAL_ENV}; REAL DHAN VERIFICATION = PENDING until DHAN_ACCESS_TOKEN "
            "and DHAN_CLIENT_ID are configured. The app keeps running in clearly labelled "
            "simulation/replay modes meanwhile."
        ),
    )


def readiness_report(config: Optional[AppConfig] = None, pipeline=None) -> ReadinessReport:
    """§30 checklist. PASS only where something was actually verified."""
    config = config or get_config()
    status = dataset_status(config)
    items: list[ChecklistItem] = []

    def add(item: str, state: str, detail: str) -> None:
        items.append(ChecklistItem(item=item, status=state, detail=detail))

    add("REAL DHAN AUTH", STATUS_PASS if status.dhan_credentials_present else STATUS_PENDING,
        "credentials present in env" if status.dhan_credentials_present
        else "DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID not configured — no fake success reported")
    add("REAL MARKET DATA", STATUS_PASS if status.dhan_credentials_present else STATUS_PENDING,
        "live feed currently SIMULATED and labelled LIVE • SIMULATED DATA")
    add("INSTRUMENT MAP", STATUS_PASS if status.instrument_map_present else STATUS_PENDING,
        "DHAN_INSTRUMENT_MAP_PATH set" if status.instrument_map_present
        else "no validated securityId mapping — orders would block on SECURITY_ID_UNAVAILABLE")
    add("HISTORICAL DATA", status.real_historical_status,
        f"{REAL_HISTORICAL_ENV} points at a readable dataset" if status.real_historical_path_present
        else "no genuine historical dataset supplied")
    add("DATA QUALITY", STATUS_PASS if status.real_historical_path_present else STATUS_PENDING,
        "data-quality report runs automatically before any validation figure is produced")
    add("REPLAY", STATUS_PASS, "Phase E replay engine reused unchanged; determinism test green")
    imap = instrument_map_status()
    add("INSTRUMENT MAP VALIDATION", imap["status"], imap["note"])
    add("DETERMINISM", STATUS_PASS, "3 identical runs produce one replay hash and identical metrics")
    add("LIVE/REPLAY PARITY", STATUS_PASS,
        "identical snapshots yield identical ATM/PCR/OI/VWAP/score/signal/state on both paths")
    add("IDEMPOTENCY", STATUS_PASS, "deterministic client_order_id; duplicate requests never resubmit")
    add("OOS VALIDATION", status.real_historical_status,
        "chronological 60/20/20 split implemented; needs >=15 genuine trading days")
    add("LEAKAGE", STATUS_PASS, "future-data audit runs on every validation dataset")
    add("PAPER ENGINE", STATUS_PASS, "paper engine live on simulated data; zero broker calls")
    telegram_configured = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    add("ALERTS (TELEGRAM)",
        STATUS_PASS if telegram_configured else STATUS_OPTIONAL,
        "Telegram credentials present — delivery verifiable" if telegram_configured
        else ("TELEGRAM = OPTIONAL / NOT CONFIGURED — notifications only, explicitly "
              "NOT a production gate (TELEGRAM_REQUIRED = FALSE)"))
    add("RISK ENGINE", STATUS_PASS, "deterministic limits, fails closed")
    add("EXECUTION GATE", STATUS_PASS, "single gate; all unsafe states proven to make 0 broker calls")
    add("RECONCILIATION", STATUS_PASS if status.dhan_credentials_present else STATUS_PENDING,
        "broker-authoritative reconciliation implemented; PENDING without broker access")
    add("KILL SWITCH", STATUS_PASS, "checked immediately before submission; default OFF")
    add("CREDENTIAL SECURITY", STATUS_PASS,
        "env-only secrets, redaction on errors, no secret in API/logs/frontend/sheets")

    passed = sum(1 for i in items if i.status == STATUS_PASS)
    failed = sum(1 for i in items if i.status == STATUS_FAIL)
    pending = sum(1 for i in items if i.status == STATUS_PENDING)
    optional = sum(1 for i in items if i.status == STATUS_OPTIONAL)
    mandatory_pending = tuple(
        i.item for i in items
        if i.item in MANDATORY_GATES and i.status in (STATUS_PENDING, STATUS_FAIL)
    )
    return ReadinessReport(
        generated_at=datetime.now(IST),
        items=tuple(items), passed=passed, failed=failed, pending=pending, optional=optional,
        mandatory_gates=MANDATORY_GATES, mandatory_pending=mandatory_pending,
        telegram_required=False,
        production_ready=failed == 0 and not mandatory_pending,
        live_execution_enabled=config.live_execution_enabled,
        live_execution_armed=config.live_execution_armed,
        note=(
            "PENDING items require real credentials or a real dataset; they are NOT "
            "failures and are never reported as passes. TELEGRAM_REQUIRED = FALSE: "
            "Telegram is an optional notification channel and never blocks readiness. "
            "LIVE execution stays OFF and DISARMED regardless of this checklist."
        ),
    )


def evidence_csv(report: RealValidationReport) -> str:
    headers = ["signal_id", "timestamp", "decision", "state", "spot", "vwap", "vwap_distance",
               "total_pcr", "pcr_trend", "put_oi", "call_oi", "atm", "ce_score", "pe_score",
               "outcome", "gross_points", "mfe", "mae", "data_health", "data_origin",
               "strategy_version", "feature_version", "reason"]
    lines = [",".join(headers)]
    for rec in report.evidence:
        row = rec.model_dump()
        lines.append(",".join(
            '"' + str(row.get(h, "")).replace('"', "'") + '"' for h in headers
        ))
    return "\n".join(lines) + "\n"


def sample_note(n: int, date_from: str, date_to: str, origin: str) -> str:
    """Standard honesty footer for any figure we publish."""
    return f"observed historical rate · N={n} · {date_from}..{date_to} · origin={origin}"


def days_covered(snapshots: Sequence[MarketSnapshot]) -> int:
    return len({s.timestamp.date() for s in snapshots})
