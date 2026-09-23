"""Phase I — real-data validation tests.

The dataset used here is a SCHEMA FIXTURE, not market data: it exercises the
ingestion/quality/audit code paths. Genuine market validation stays PENDING
until a real dataset and real credentials are supplied.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from engines.fixtures import default_config
from lib.dates import IST
from models.research_models import (
    ORIGIN_LIVE_REAL,
    ORIGIN_REAL_HISTORICAL,
    ORIGIN_REPLAY_SYNTHETIC,
    STATUS_PENDING,
)
from research.analytics import build_evidence, compute_analytics
from research.audits import audit_determinism, audit_leakage, audit_session_reset
from research.ingest import (
    RealDataError,
    build_descriptor,
    dataset_hash,
    in_memory_source,
    load_real_source,
    snapshot_fingerprint,
    validate_csv_schema,
)
from research.quality import assess_quality
from research.validation import (
    dataset_status,
    evidence_csv,
    readiness_report,
    validate_dataset,
)
from replay.backtest_engine import run_replay
from replay.fixtures import synthetic_session
from server import app

CFG = default_config()

HEADERS = ["snapshot_id", "timestamp", "instrument", "open", "high", "low", "close", "volume",
           "expiry", "strike", "ce_ltp", "ce_oi", "ce_oi_change", "ce_volume", "ce_iv",
           "pe_ltp", "pe_oi", "pe_oi_change", "pe_volume", "pe_iv"]


def write_csv(path, days=("2025-01-15",), minutes=40, drop_column=None, duplicate=False):
    """Writes a schema-shaped option-chain file (fixture, not market data)."""
    rows = []
    for day in days:
        base = datetime.fromisoformat(f"{day}T11:00:00+05:30")
        spot = 25000.0
        for i in range(minutes):
            ts = base + timedelta(minutes=i)
            spot += 4.0
            sid = f"{day}-{i:03d}"
            for k, strike in enumerate(range(24900, 25201, 50)):
                rows.append({
                    "snapshot_id": sid, "timestamp": ts.isoformat(), "instrument": "NIFTY",
                    "open": spot - 5, "high": spot + 6, "low": spot - 8, "close": spot,
                    "volume": 120000 + i * 10, "expiry": "2025-01-30", "strike": strike,
                    "ce_ltp": round(max(1.0, 120 - k * 12 + i * 0.6), 2),
                    "ce_oi": 100000 + k * 1000 - i * 40,
                    "ce_oi_change": -40 - i, "ce_volume": 5000 + i, "ce_iv": 12.5,
                    "pe_ltp": round(max(1.0, 40 + k * 10 - i * 0.2), 2),
                    "pe_oi": 150000 + k * 1500 + i * 120,
                    "pe_oi_change": 120 + i, "pe_volume": 6000 + i, "pe_iv": 13.1,
                })
    if duplicate:
        rows.extend(rows[:7])
    headers = [h for h in HEADERS if h != drop_column]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


@pytest.fixture
def real_csv(tmp_path):
    return write_csv(tmp_path / "nifty_real.csv")


# =========================== schema / ingestion ===========================
def test_real_csv_schema_validates(real_csv):
    ok, missing, optional_missing = validate_csv_schema(real_csv)
    assert ok is True and missing == []
    assert "expiry" not in optional_missing  # the fixture carries expiry


def test_missing_required_field_is_no_data_not_inferred(tmp_path):
    path = write_csv(tmp_path / "broken.csv", drop_column="ce_oi")
    ok, missing, _ = validate_csv_schema(path)
    assert ok is False and "ce_oi" in missing
    with pytest.raises(RealDataError) as exc:
        load_real_source(path)
    assert "NO_DATA" in str(exc.value)


def test_missing_file_and_unsupported_format_are_reported(tmp_path):
    with pytest.raises(RealDataError):
        load_real_source(tmp_path / "nope.csv")
    bad = tmp_path / "data.parquet"
    bad.write_bytes(b"x")
    with pytest.raises(RealDataError) as exc:
        load_real_source(bad)
    assert "NO_DATA" in str(exc.value)


def test_real_source_loads_snapshots_through_the_existing_pipeline(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    assert len(snaps) == 40
    assert snaps[0].instrument == "NIFTY"
    assert len(snaps[0].chain.rows) == 7
    assert snaps[0].chain.rows[0].ce.oi is not None


# =========================== dataset versioning ===========================
def test_dataset_hash_is_content_addressed_and_order_independent(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    assert dataset_hash(snaps) == dataset_hash(list(reversed(snaps)))
    assert snapshot_fingerprint(snaps[0]) != snapshot_fingerprint(snaps[1])


def test_descriptor_carries_full_provenance(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    desc = build_descriptor(snaps, "nifty_real.csv", CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    assert desc.data_origin == ORIGIN_REAL_HISTORICAL
    assert desc.dataset_hash and desc.dataset_id.startswith("real_historical-")
    assert desc.strategy_version == CFG.strategy_version
    assert desc.feature_version == CFG.feature_engine_version
    assert desc.trading_days == 1 and desc.snapshot_count == 40
    assert desc.schema_version == "1.0"


def test_data_origin_labels_are_never_mixed(real_csv):
    real = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    synth = validate_dataset(list(synthetic_session()), CFG, data_origin=ORIGIN_REPLAY_SYNTHETIC)
    assert real.data_origin == ORIGIN_REAL_HISTORICAL
    assert real.dataset.data_origin == ORIGIN_REAL_HISTORICAL
    assert synth.data_origin == ORIGIN_REPLAY_SYNTHETIC
    assert any("NOT REAL MARKET DATA" in n for n in synth.notes)
    assert ORIGIN_LIVE_REAL != ORIGIN_REPLAY_SYNTHETIC


# =========================== data quality =================================
def test_quality_report_covers_every_required_check(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    report = assess_quality(snaps, "ds-1", ORIGIN_REAL_HISTORICAL, expected_interval_seconds=60)
    assert report.snapshot_count == 40 and report.trading_days == 1
    assert report.duplicate_snapshots == 0 and report.duplicate_timestamps == 0
    assert report.out_of_order_snapshots == 0 and report.invalid_values == 0
    assert report.missing_oi == 0 and report.missing_ltp == 0 and report.missing_strikes == 0
    assert report.expiry_coverage == ("2025-01-30",)
    assert report.strikes_per_snapshot_min == report.strikes_per_snapshot_max == 7
    assert report.production_quality is True and report.failures == ()


def test_quality_report_flags_duplicates_and_out_of_order(tmp_path):
    path = write_csv(tmp_path / "dupes.csv", duplicate=True)
    snaps = list(load_real_source(path).iter_snapshots())
    shuffled = snaps[5:] + snaps[:5]
    report = assess_quality(shuffled, "ds-2", ORIGIN_REAL_HISTORICAL)
    assert report.out_of_order_snapshots >= 1
    assert report.production_quality is False
    assert report.failures


def test_empty_dataset_is_no_data_not_zero_quality():
    report = assess_quality([], "ds-empty", ORIGIN_REAL_HISTORICAL)
    assert report.production_quality is False
    assert "NO_DATA" in report.failures[0]


def test_tiny_dataset_fails_the_minimum_sample_gate(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())[:3]
    report = assess_quality(snaps, "ds-tiny", ORIGIN_REAL_HISTORICAL)
    assert report.production_quality is False
    assert any("minimum" in f for f in report.failures)


# =========================== replay + audits ==============================
def test_real_data_replay_uses_the_existing_phase_e_engine(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    result = run_replay(in_memory_source(snaps), CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    assert result.metrics.data_origin == ORIGIN_REAL_HISTORICAL
    assert result.metrics.strategy_version == CFG.strategy_version
    assert len(result.signals) == len(snaps)
    source = open("/app/backend/research/validation.py").read()
    assert "run_replay" in source
    for banned in ("pcr_engine", "score_engine", "rule_engine", "vwap_engine", "state_machine"):
        assert banned not in source


def test_future_leakage_audit_passes_on_real_data(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    result = run_replay(in_memory_source(snaps), CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    audit = audit_leakage(result.signals, result.outcomes, snaps)
    assert audit.passed is True
    assert audit.future_feature_reads == 0 and audit.future_outcome_inputs == 0
    assert audit.checked_signals == len(snaps)


def test_leakage_audit_detects_an_injected_future_read(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    result = run_replay(in_memory_source(snaps), CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    tampered = [s.model_copy(update={"timestamp": s.timestamp - timedelta(minutes=30)})
                for s in result.signals]
    audit = audit_leakage(tampered, result.outcomes, snaps)
    assert audit.passed is False and audit.future_feature_reads > 0


def test_session_reset_audit_over_two_trading_days(tmp_path):
    path = write_csv(tmp_path / "twodays.csv", days=("2025-01-15", "2025-01-16"))
    snaps = list(load_real_source(path).iter_snapshots())
    audit = audit_session_reset(snaps, CFG)
    assert audit.days == ("2025-01-15", "2025-01-16")
    assert audit.combined_matches_per_day is True
    assert audit.cooldown_carryover is False
    assert audit.passed is True


def test_determinism_audit_is_green(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    audit = audit_determinism(snaps, CFG, ORIGIN_REAL_HISTORICAL)
    assert audit.dataset_hash_stable and audit.replay_hash_stable
    assert audit.metrics_stable and audit.signal_count_stable and audit.passed


def test_same_dataset_reproduces_identical_report(real_csv):
    first = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    second = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    assert first.dataset.dataset_hash == second.dataset.dataset_hash
    assert first.replay_hash == second.replay_hash
    assert first.metrics == second.metrics
    assert len(first.evidence) == len(second.evidence)


# =========================== analytics / honesty ==========================
def test_analytics_report_sample_sizes_and_denominators(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    result = run_replay(in_memory_source(snaps), CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    analytics = compute_analytics(result.signals, result.outcomes, CFG, ORIGIN_REAL_HISTORICAL)
    assert analytics.sample_size_signals >= 0
    assert "resolved" in analytics.by_side[0].denominator
    assert "not a forecast" in analytics.expectancy_definition
    assert "peak-to-trough" in analytics.drawdown_definition
    assert f"N={analytics.sample_size_resolved}" in analytics.note
    assert len(analytics.by_score_bucket) == 5
    assert len(analytics.by_time_of_day) == len(CFG.backtest_time_windows)
    for bucket in analytics.by_side + analytics.by_score_bucket:
        assert bucket.n >= bucket.resolved  # never more resolved than signals


def test_analytics_never_uses_probability_or_accuracy_language(real_csv):
    report = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    import re

    blob = report.model_dump_json().lower()
    for banned in ("guaranteed", "sure shot", "high probability", "win rate"):
        assert banned not in blob, banned
    assert not re.search(r"\d\s*%\s*(probability|accurate|accuracy)", blob)
    # "accuracy"/"probability" may appear ONLY inside an explicit denial
    for word in ("accuracy", "probability"):
        for match in re.finditer(word, blob):
            prefix = blob[max(0, match.start() - 40):match.start()]
            assert ("not " in prefix or "never" in prefix or "nor " in prefix
                    or "or " in prefix), (word, prefix[-40:])
    assert "measured statistics" in blob


def test_evidence_record_makes_a_signal_traceable(real_csv):
    snaps = list(load_real_source(real_csv).iter_snapshots())
    result = run_replay(in_memory_source(snaps), CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    evidence = build_evidence(result.signals, result.outcomes, ORIGIN_REAL_HISTORICAL)
    setups = [s for s in result.signals if s.decision in ("CE_SETUP", "PE_SETUP")]
    assert len(evidence) >= len(setups)
    if evidence:
        rec = evidence[0]
        assert rec.signal_id and rec.reason
        assert rec.data_origin == ORIGIN_REAL_HISTORICAL
        assert rec.strategy_version and rec.feature_version
        assert rec.max_score == 100
        assert isinstance(rec.rule_results, tuple)


def test_evidence_csv_export_has_no_new_dependency(real_csv):
    report = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    csv_text = evidence_csv(report)
    assert csv_text.splitlines()[0].startswith("signal_id,timestamp,decision")


def test_validation_notes_declare_no_optimization(real_csv):
    report = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    joined = " ".join(report.notes)
    assert "no optimization" in joined.lower()
    assert "frozen" in joined.lower()
    assert report.dataset.strategy_version == CFG.strategy_version


# =========================== pending / security ===========================
def test_real_data_status_is_pending_without_credentials(monkeypatch):
    monkeypatch.delenv("REAL_HISTORICAL_PATH", raising=False)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    status = dataset_status(CFG)
    assert status.real_historical_status == STATUS_PENDING
    assert status.live_real_data_status == STATUS_PENDING
    assert status.dhan_credentials_present is False
    assert "PENDING" in status.note


def test_readiness_checklist_reports_pending_honestly(monkeypatch):
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("REAL_HISTORICAL_PATH", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    report = readiness_report(CFG)
    names = {i.item: i.status for i in report.items}
    assert names["REAL DHAN AUTH"] == STATUS_PENDING
    assert names["HISTORICAL DATA"] == STATUS_PENDING
    assert names["ALERTS (TELEGRAM)"] == "OPTIONAL"  # Telegram is not a production gate
    assert names["EXECUTION GATE"] == "PASS"
    assert names["KILL SWITCH"] == "PASS"
    assert names["CREDENTIAL SECURITY"] == "PASS"
    assert report.production_ready is False
    assert report.live_execution_enabled is False and report.live_execution_armed is False
    assert report.pending >= 1 and report.failed == 0
    assert report.telegram_required is False


def test_research_layer_never_reads_credentials_into_output(real_csv, monkeypatch):
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "SUPER-SECRET-TOKEN")
    monkeypatch.setenv("DHAN_CLIENT_ID", "CLIENT-SECRET-ID")
    report = validate_dataset(real_csv, CFG, data_origin=ORIGIN_REAL_HISTORICAL)
    blob = report.model_dump_json()
    assert "SUPER-SECRET-TOKEN" not in blob and "CLIENT-SECRET-ID" not in blob
    status = dataset_status(CFG).model_dump_json()
    assert "SUPER-SECRET-TOKEN" not in status and "CLIENT-SECRET-ID" not in status


def test_research_layer_has_no_execution_surface():
    import pathlib

    for file in pathlib.Path("/app/backend/research").rglob("*.py"):
        low = file.read_text().lower()
        for banned in ("place_order", "submit_order", "dhanexecutionadapter", "buy_order",
                       "sell_order", "modify_order", "cancel_order"):
            assert banned not in low, (file.name, banned)


def test_paper_engine_can_consume_real_data_labels():
    from paper.paper_engine import PaperTradingEngine

    engine = PaperTradingEngine(CFG, data_origin=ORIGIN_LIVE_REAL)
    summary = engine.summary()
    assert summary.data_origin == ORIGIN_LIVE_REAL
    assert "PAPER" in summary.label.upper()
    assert engine.data_origin == ORIGIN_LIVE_REAL


# =========================== API ==========================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_research_status_and_readiness_endpoints(client):
    status = client.get("/api/research/status")
    assert status.status_code == 200
    assert status.json()["real_historical_status"] in ("PASS", "PENDING")

    readiness = client.get("/api/research/readiness")
    assert readiness.status_code == 200
    body = readiness.json()
    assert body["live_execution_enabled"] is False
    assert body["live_execution_armed"] is False
    assert len(body["items"]) == 19  # Phase J added 5 checklist items
    # env VARIABLE names are fine; a secret VALUE must never appear
    import os

    token = os.environ.get("DHAN_ACCESS_TOKEN", "")
    assert not token or token not in readiness.text


def test_validate_endpoint_without_dataset_is_409(client, monkeypatch):
    monkeypatch.delenv("REAL_HISTORICAL_PATH", raising=False)
    res = client.post("/api/research/validate", json={})
    assert res.status_code == 409
    assert "PENDING" in res.json()["detail"]


def test_validate_endpoint_runs_on_a_supplied_dataset(client, real_csv):
    res = client.post("/api/research/validate",
                      json={"path": real_csv, "data_origin": ORIGIN_REAL_HISTORICAL})
    assert res.status_code == 200
    body = res.json()
    assert body["dataset"]["data_origin"] == ORIGIN_REAL_HISTORICAL
    assert body["quality"]["production_quality"] is True
    assert body["leakage"]["passed"] is True
    assert body["determinism"]["passed"] is True
    assert body["session_reset"]["passed"] is True
    assert body["metrics"]["data_origin"] == ORIGIN_REAL_HISTORICAL

    again = client.get("/api/research/report")
    assert again.status_code == 200
    assert again.json()["replay_hash"] == body["replay_hash"]


def test_validate_endpoint_rejects_a_broken_dataset(client, tmp_path):
    broken = write_csv(tmp_path / "broken2.csv", drop_column="pe_ltp")
    res = client.post("/api/research/validate", json={"path": broken})
    assert res.status_code == 409
    assert "NO_DATA" in res.json()["detail"]


def test_evidence_export_endpoint(client, real_csv):
    res = client.post("/api/research/export/evidence", json={"path": real_csv})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0].startswith("signal_id,")


def test_research_api_is_readonly_for_state(client):
    assert client.get("/api/research/nope").status_code == 404
    assert client.put("/api/research/status").status_code == 405
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        if path.startswith("/api/research"):
            assert set(methods) <= {"get", "post"}, (path, methods)


def test_live_execution_defaults_unchanged_by_phase_i():
    fresh = default_config()
    assert fresh.system_mode == "RESEARCH"
    assert fresh.live_execution_enabled is False
    assert fresh.live_execution_armed is False
    assert os.environ.get("LIVE_EXECUTION_ENABLED", "false").lower() != "true"
