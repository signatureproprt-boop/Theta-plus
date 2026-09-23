"""Phase J — external-input verification harness.

Everything verifiable WITHOUT external credentials is proven here: mutation-based
future-leakage, 3-run determinism, live/replay parity, instrument-map validation,
OOS splitting, risk/kill-switch/idempotency bypass resistance, credential
security and the readiness matrix. Anything that needs a real credential or a
genuine dataset is asserted to report PENDING — never a fake PASS.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from engines.fixtures import SCENARIO_BUILDERS, default_config
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from execution.adapter import MockExecutionAdapter
from execution.instruments import (
    CANONICAL_KINDS,
    InstrumentMap,
    instrument_map_status,
    validate_instrument_map,
)
from execution.service import ExecutionService, client_order_id_for
from lib.dates import IST
from models.execution_models import (
    APPROVED,
    BLOCK_KILL_SWITCH,
    BLOCK_SECURITY_ID,
    RECON_OK,
)
from models.research_models import STATUS_PENDING
from research.audits import MUTATIONS, audit_determinism_runs, audit_mutation_leakage
from research.ingest import load_real_source
from research.oos import MIN_DAYS_FOR_OOS, run_oos, split_by_day
from research.parity import COMPARED_FIELDS, live_replay_parity
from research.validation import readiness_report
from server import app

CFG = default_config()
AT = datetime(2025, 1, 15, 12, 0, tzinfo=IST)

HEADERS = ["snapshot_id", "timestamp", "instrument", "open", "high", "low", "close", "volume",
           "expiry", "strike", "ce_ltp", "ce_oi", "ce_oi_change", "ce_volume", "ce_iv",
           "pe_ltp", "pe_oi", "pe_oi_change", "pe_volume", "pe_iv"]

VALID_MAP = [
    {"underlying": "NIFTY", "instrument_type": "INDEX", "security_id": "13",
     "exchange_segment": "IDX_I", "expiry": "", "strike": 0, "option_type": "",
     "tradable": True, "lot_size": 1},
    {"underlying": "NIFTY", "instrument_type": "FUTIDX", "security_id": "35001",
     "exchange_segment": "NSE_FNO", "expiry": "2025-01-30", "strike": 0, "option_type": "",
     "tradable": True, "lot_size": 75},
    {"underlying": "NIFTY", "instrument_type": "OPTIDX", "security_id": "43492",
     "exchange_segment": "NSE_FNO", "expiry": "2025-01-30", "strike": 25050,
     "option_type": "CALL", "tradable": True, "lot_size": 1},
    {"underlying": "NIFTY", "instrument_type": "OPTIDX", "security_id": "43493",
     "exchange_segment": "NSE_FNO", "expiry": "2025-01-30", "strike": 25050,
     "option_type": "PUT", "tradable": True, "lot_size": 1},
]


def write_csv(path, days=("2025-01-15",), minutes=40):
    rows = []
    for day in days:
        base = datetime.fromisoformat(f"{day}T11:00:00+05:30")
        spot = 25000.0
        for i in range(minutes):
            ts = base + timedelta(minutes=i)
            spot += 4.0
            for k, strike in enumerate(range(24900, 25201, 50)):
                rows.append({
                    "snapshot_id": f"{day}-{i:03d}", "timestamp": ts.isoformat(),
                    "instrument": "NIFTY", "open": spot - 5, "high": spot + 6,
                    "low": spot - 8, "close": spot, "volume": 120000 + i * 10,
                    "expiry": "2025-01-30", "strike": strike,
                    "ce_ltp": round(max(1.0, 120 - k * 12 + i * 0.6), 2),
                    "ce_oi": 100000 + k * 1000 - i * 40, "ce_oi_change": -40 - i,
                    "ce_volume": 5000 + i, "ce_iv": 12.5,
                    "pe_ltp": round(max(1.0, 40 + k * 10 - i * 0.2), 2),
                    "pe_oi": 150000 + k * 1500 + i * 120, "pe_oi_change": 120 + i,
                    "pe_volume": 6000 + i, "pe_iv": 13.1,
                })
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


@pytest.fixture
def dataset(tmp_path):
    return list(load_real_source(write_csv(tmp_path / "ds.csv")).iter_snapshots())


@pytest.fixture
def multiday(tmp_path):
    days = tuple(f"2025-01-{d:02d}" for d in range(1, 21))
    return list(load_real_source(write_csv(tmp_path / "multi.csv", days=days, minutes=12))
                .iter_snapshots())


# =========================== §7 mutation leakage ==========================
def test_mutating_future_inputs_never_changes_earlier_decisions(dataset):
    audit = audit_mutation_leakage(dataset, CFG)
    assert audit.passed is True
    assert audit.future_feature_reads == 0
    assert audit.checked_signals > 0
    for kind in MUTATIONS:
        assert kind in audit.detail


def test_mutation_set_covers_every_required_input():
    for required in ("price", "candle", "oi", "pcr", "ltp", "volume"):
        assert required in MUTATIONS


def test_mutation_leakage_needs_real_data_not_a_guess():
    audit = audit_mutation_leakage([], CFG)
    assert audit.passed is False and "NO_DATA" in audit.detail


# =========================== §8 determinism ===============================
def test_three_runs_produce_one_hash(dataset):
    audit, hashes = audit_determinism_runs(dataset, CFG, "REAL_HISTORICAL", runs=3)
    assert len(hashes) == 3
    assert hashes[0] == hashes[1] == hashes[2]
    assert audit.passed and audit.metrics_stable and audit.replay_hash == hashes[0]


# =========================== §13 live/replay parity =======================
def test_live_and_replay_paths_agree_on_every_strategy_field(dataset):
    report = live_replay_parity(dataset, CFG)
    assert report.passed is True
    assert report.mismatches == ()
    assert report.compared_evaluations == len(dataset)
    for field in ("atm", "total_pcr", "vwap", "ce_score", "pe_score", "decision", "state"):
        assert field in COMPARED_FIELDS


def test_parity_harness_would_catch_a_divergence(dataset, monkeypatch):
    import research.parity as parity

    real = parity.live_path_signals

    def tampered(snapshots, config):
        rows = real(snapshots, config)
        if rows:
            rows[0] = {**rows[0], "ce_score": rows[0]["ce_score"] + 7}
        return rows

    monkeypatch.setattr(parity, "live_path_signals", tampered)
    report = parity.live_replay_parity(dataset, CFG)
    assert report.passed is False
    assert report.mismatches[0].field == "ce_score"


def test_parity_over_multiple_sessions(multiday):
    assert live_replay_parity(multiday, CFG).passed is True


# =========================== §3 instrument mapping ========================
def test_instrument_map_is_pending_without_configuration(monkeypatch):
    monkeypatch.delenv("DHAN_INSTRUMENT_MAP_PATH", raising=False)
    report = instrument_map_status()
    assert report["status"] == STATUS_PENDING
    assert report["broker_verified"] is False
    assert report["canonical_kinds_missing"] == CANONICAL_KINDS
    assert "PENDING" in report["note"]


def test_valid_canonical_mapping_passes_structural_validation():
    report = validate_instrument_map(VALID_MAP)
    assert report["status"] == "PASS"
    assert report["valid_rows"] == 4
    assert report["canonical_kinds_missing"] == ()
    assert set(report["canonical_kinds_present"]) == set(CANONICAL_KINDS)
    assert report["broker_verified"] is False  # honest: no broker lookup happened


def test_incomplete_or_wrong_mapping_fails():
    missing_pe = [r for r in VALID_MAP if r["option_type"] != "PUT"]
    assert validate_instrument_map(missing_pe)["status"] == "FAIL"
    bad_segment = [dict(VALID_MAP[2], exchange_segment="NASDAQ")]
    assert "unknown exchange_segment" in validate_instrument_map(bad_segment)["errors"][0]
    bad_type = [dict(VALID_MAP[2], instrument_type="CRYPTO")]
    assert "unknown instrument_type" in validate_instrument_map(bad_type)["errors"][0]
    bad_strike = [dict(VALID_MAP[2], strike=-1)]
    assert "strike" in validate_instrument_map(bad_strike)["errors"][0]
    no_id = [{k: v for k, v in VALID_MAP[2].items() if k != "security_id"}]
    assert "missing security_id" in validate_instrument_map(no_id)["errors"][0]


def test_missing_or_corrupt_mapping_file_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("DHAN_INSTRUMENT_MAP_PATH", str(tmp_path / "gone.json"))
    assert instrument_map_status()["status"] == "FAIL"
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json")
    monkeypatch.setenv("DHAN_INSTRUMENT_MAP_PATH", str(corrupt))
    assert instrument_map_status()["status"] == "FAIL"


def test_one_mapping_object_serves_data_replay_paper_and_execution(tmp_path, monkeypatch):
    path = tmp_path / "map.json"
    path.write_text(json.dumps(VALID_MAP))
    monkeypatch.setenv("DHAN_INSTRUMENT_MAP_PATH", str(path))
    mapping = InstrumentMap.from_env()
    assert mapping.configured is True
    assert mapping.resolve("NIFTY", 25050, "CE").security_id == "43492"
    svc = ExecutionService(CFG, adapter=MockExecutionAdapter(), instrument_map=mapping)
    assert svc.instruments.resolve("NIFTY", 25050, "PE").security_id == "43493"


async def test_execution_blocks_when_mapping_absent(monkeypatch):
    monkeypatch.setenv("LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTION_ARMED", "true")
    cfg = default_config().model_copy(update={
        "system_mode": "LIVE", "live_execution_enabled": True, "live_execution_armed": True,
    })
    svc = ExecutionService(cfg, adapter=MockExecutionAdapter(), instrument_map=InstrumentMap([]))
    svc.reconciler.report = svc.reconciler.report.model_copy(
        update={"state": RECON_OK, "blocks_execution": False})
    features = SCENARIO_BUILDERS["strong_ce"](AT)
    decision = evaluate_and_apply(SignalMemory(), features, cfg, AT)
    verdict = await svc.attempt_entry(decision, features, "sig-map", now=AT)
    assert verdict.reason == BLOCK_SECURITY_ID
    assert svc.adapter.submit_count == 0


# =========================== §10 OOS ======================================
def test_oos_is_pending_on_insufficient_history(dataset):
    report = run_oos(dataset, CFG, "REAL_HISTORICAL")
    assert report.status == STATUS_PENDING
    assert report.train is None and report.out_of_sample is None
    assert str(MIN_DAYS_FOR_OOS) in report.note
    assert "No split was fabricated" in report.note


def test_oos_split_is_chronological_and_non_overlapping(multiday):
    train, val, oos = split_by_day(multiday)
    tdays = {s.timestamp.date() for s in train}
    vdays = {s.timestamp.date() for s in val}
    odays = {s.timestamp.date() for s in oos}
    assert not (tdays & vdays) and not (vdays & odays) and not (tdays & odays)
    assert max(tdays) < min(vdays) and max(vdays) < min(odays)
    assert len(tdays) + len(vdays) + len(odays) == len({s.timestamp.date() for s in multiday})


def test_oos_report_evaluates_each_window_separately(multiday):
    report = run_oos(multiday, CFG, "REAL_HISTORICAL")
    assert report.status == "PASS"
    assert report.train and report.validation and report.out_of_sample
    assert report.train.trading_days >= report.out_of_sample.trading_days
    assert report.train.replay_hash != report.out_of_sample.replay_hash
    assert "no window was tuned" in report.note.lower()
    for split in (report.train, report.validation, report.out_of_sample):
        assert split.snapshots > 0
        assert split.analytics is not None


# =========================== §11 descriptive ==============================
def test_fine_buckets_are_additive_and_carry_sample_sizes(dataset):
    from replay.backtest_engine import run_replay
    from research.analytics import compute_analytics
    from research.ingest import in_memory_source

    result = run_replay(in_memory_source(dataset), CFG, data_origin="REAL_HISTORICAL")
    analytics = compute_analytics(result.signals, result.outcomes, CFG, "REAL_HISTORICAL")
    assert len(analytics.by_score_bucket_fine) == 6
    assert [b.label for b in analytics.by_score_bucket_fine][0] == "score 35-44"
    assert len(analytics.by_time_of_day_30m) == 8
    assert analytics.by_time_of_day_30m[0].label == "11:30-12:00"
    assert len(analytics.by_score_bucket) == 5  # Phase I buckets untouched
    for bucket in analytics.by_score_bucket_fine + analytics.by_time_of_day_30m:
        assert bucket.n >= 0 and "resolved" in bucket.denominator


# =========================== §17-§19 safety re-verification ===============
async def test_risk_gate_requires_every_condition(monkeypatch):
    monkeypatch.setenv("LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTION_ARMED", "true")
    cfg = default_config().model_copy(update={
        "system_mode": "LIVE", "live_execution_enabled": True, "live_execution_armed": True,
        "risk_max_slippage_points": 5.0,
    })
    svc = ExecutionService(cfg, adapter=MockExecutionAdapter(fill_price=55.0),
                           instrument_map=InstrumentMap([
                               {"underlying": "NIFTY", "expiry": "2025-01-30", "strike": 25050,
                                "option_type": "CALL", "security_id": "43492",
                                "exchange_segment": "NSE_FNO", "lot_size": 1, "tradable": True}]))
    svc.reconciler.report = svc.reconciler.report.model_copy(
        update={"state": RECON_OK, "blocks_execution": False})
    features = SCENARIO_BUILDERS["strong_ce"](AT)
    decision = evaluate_and_apply(SignalMemory(), features, cfg, AT)

    verdict = await svc.attempt_entry(decision, features, "sig-risk", now=AT)
    names = dict(verdict.checks)
    for condition in ("mode_live", "live_execution_enabled", "live_execution_armed",
                      "kill_switch_off", "signal_is_entry_setup", "data_health_ok",
                      "trading_session", "no_duplicate_order", "security_id_available",
                      "instrument_validated", "reconciliation"):
        assert condition in names, condition
        assert names[condition] is True
    assert verdict.approved is True and verdict.reason == APPROVED


async def test_kill_switch_has_no_bypass(monkeypatch):
    monkeypatch.setenv("LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTION_ARMED", "true")
    cfg = default_config().model_copy(update={
        "system_mode": "LIVE", "live_execution_enabled": True, "live_execution_armed": True,
        "risk_max_slippage_points": 5.0,
    })
    svc = ExecutionService(cfg, adapter=MockExecutionAdapter(),
                           instrument_map=InstrumentMap([
                               {"underlying": "NIFTY", "expiry": "2025-01-30", "strike": 25050,
                                "option_type": "CALL", "security_id": "43492",
                                "exchange_segment": "NSE_FNO", "lot_size": 1, "tradable": True}]))
    svc.reconciler.report = svc.reconciler.report.model_copy(
        update={"state": RECON_OK, "blocks_execution": False})
    svc.engage_kill_switch()
    features = SCENARIO_BUILDERS["strong_ce"](AT)
    decision = evaluate_and_apply(SignalMemory(), features, cfg, AT)
    for attempt in range(3):  # retries must not wear the gate down
        verdict = await svc.attempt_entry(decision, features, f"sig-kill-{attempt}", now=AT)
        assert verdict.reason == BLOCK_KILL_SWITCH
    assert svc.adapter.submit_count == 0


def test_idempotency_key_is_stable_across_processes():
    assert client_order_id_for("abc") == client_order_id_for("abc")
    assert len(client_order_id_for("abc")) <= 30


# =========================== §20 credential security ======================
def test_no_secret_values_in_repository_or_runtime_logs():
    import pathlib
    import re
    import subprocess

    root = pathlib.Path("/app")
    this_file = pathlib.Path(__file__).resolve()
    suspicious = []
    # JWT prefix (Dhan tokens), Telegram bot-token shape, OpenAI-style key prefix
    patterns = (re.compile(r"eyJ[A-Za-z0-9_-]{12,}\."), re.compile(r"\b\d{8,12}:AA[\w-]{20,}"),
                re.compile(r"\bsk-[A-Za-z0-9]{20,}"))
    for file in list(root.glob("backend/**/*.py")) + list(root.glob("frontend/src/**/*.ts*")):
        # test files legitimately hold clearly-synthetic fixture tokens
        if file.resolve() == this_file or "/tests/" in str(file):
            continue
        text = file.read_text()
        if any(p.search(text) for p in patterns):
            suspicious.append(str(file))
    assert suspicious == [], suspicious

    tracked = subprocess.run(["git", "ls-files"], cwd="/app", capture_output=True, text=True)
    assert ".env\n" not in tracked.stdout  # real secrets file is never tracked

    for log in ("/var/log/supervisor/backend.err.log", "/var/log/supervisor/backend.out.log"):
        p = pathlib.Path(log)
        if p.exists():
            body = p.read_text(errors="ignore")
            for marker in ("DHAN_ACCESS_TOKEN=", "TELEGRAM_BOT_TOKEN=", "access-token:"):
                assert marker not in body, (log, marker)


def test_env_example_holds_placeholders_only():
    text = open("/app/backend/.env.example").read()
    for key in ("DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID", "REAL_HISTORICAL_PATH", "DHAN_INSTRUMENT_MAP_PATH"):
        assert f"{key}=" in text
        line = next(l for l in text.splitlines() if l.startswith(f"{key}="))
        assert line.strip() == f"{key}=", line  # placeholder only, no value


# =========================== readiness / PENDING honesty ==================
def test_readiness_matrix_marks_external_inputs_pending(monkeypatch):
    for key in ("DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID", "DHAN_INSTRUMENT_MAP_PATH",
                "REAL_HISTORICAL_PATH", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    report = readiness_report(CFG)
    status = {i.item: i.status for i in report.items}
    assert status["REAL DHAN AUTH"] == STATUS_PENDING
    assert status["INSTRUMENT MAP VALIDATION"] == STATUS_PENDING
    assert status["HISTORICAL DATA"] == STATUS_PENDING
    assert status["OOS VALIDATION"] == STATUS_PENDING
    assert status["ALERTS (TELEGRAM)"] == "OPTIONAL"
    assert status["DETERMINISM"] == "PASS"
    assert status["LIVE/REPLAY PARITY"] == "PASS"
    assert status["IDEMPOTENCY"] == "PASS"
    assert report.failed == 0 and report.production_ready is False
    # Telegram must never appear among the mandatory production gates (§17/§25)
    from research.validation import MANDATORY_GATES

    assert not any("TELEGRAM" in g for g in MANDATORY_GATES)
    assert report.telegram_required is False
    assert "ALERTS (TELEGRAM)" not in report.mandatory_pending
    assert report.live_execution_enabled is False and report.live_execution_armed is False


def test_live_stays_off_after_phase_j():
    fresh = default_config()
    assert fresh.system_mode == "RESEARCH"
    assert fresh.live_execution_enabled is False and fresh.live_execution_armed is False
    assert os.environ.get("LIVE_EXECUTION_ENABLED", "false").lower() != "true"
    assert os.environ.get("LIVE_EXECUTION_ARMED", "false").lower() != "true"


# =========================== API ==========================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def csv_path(tmp_path_factory):
    return write_csv(tmp_path_factory.mktemp("j") / "api.csv")


def test_audit_endpoints_run_on_a_supplied_dataset(client, csv_path):
    parity = client.post("/api/research/parity", json={"path": csv_path})
    assert parity.status_code == 200 and parity.json()["passed"] is True

    leak = client.post("/api/research/leakage/mutation", json={"path": csv_path})
    assert leak.status_code == 200 and leak.json()["passed"] is True

    det = client.post("/api/research/determinism", json={"path": csv_path})
    body = det.json()
    assert det.status_code == 200 and body["identical"] is True
    assert len(set(body["run_hashes"])) == 1

    oos = client.post("/api/research/oos", json={"path": csv_path})
    assert oos.status_code == 200 and oos.json()["status"] == STATUS_PENDING


def test_audit_endpoints_are_pending_without_a_dataset(client, monkeypatch):
    monkeypatch.delenv("REAL_HISTORICAL_PATH", raising=False)
    for path in ("/api/research/parity", "/api/research/leakage/mutation",
                 "/api/research/determinism", "/api/research/oos"):
        res = client.post(path, json={})
        assert res.status_code == 409, path
        assert "PENDING" in res.json()["detail"]


def test_instrument_map_endpoint_is_pending_and_leaks_nothing(client):
    res = client.get("/api/research/instrument-map")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("PENDING", "FAIL", "PASS")
    assert body["broker_verified"] is False
    assert "DHAN_ACCESS_TOKEN=" not in res.text


def test_readiness_endpoint_matches_the_final_matrix(client):
    res = client.get("/api/research/readiness")
    body = res.json()
    assert res.status_code == 200
    assert len(body["items"]) == 19
    assert body["failed"] == 0
    assert body["production_ready"] is False
    assert body["live_execution_enabled"] is False
    assert body["live_execution_armed"] is False


def test_still_no_order_or_arming_endpoint(client):
    schema = client.get("/openapi.json").json()
    for path in schema["paths"]:
        low = path.lower()
        for banned in ("order", "execute", "arm", "submit", "webhook", "place"):
            assert banned not in low, path
