"""Phase D — pipeline integration + control-room API (§1/§14/§16/§22/§23).

The pipeline wires feed -> normalizer -> feature engine -> signal engine ->
dashboard/writers WITHOUT the engines ever touching Google APIs, and keeps
running when the feed or Sheets fail.
"""

from datetime import datetime

import pytest

from engines.fixtures import default_config
from lib.dates import IST
from pipeline import Pipeline

MID_SESSION = datetime(2025, 1, 15, 12, 30, 0, tzinfo=IST)
BEFORE_START = datetime(2025, 1, 15, 10, 15, 0, tzinfo=IST)


def _pipeline() -> Pipeline:
    return Pipeline(default_config())


async def test_feed_failure_clears_previous_actionable_signal():
    p = _pipeline()
    await p.tick(MID_SESSION)
    assert p.decision is not None and p.features is not None
    p.decision = p.decision.model_copy(update={"decision": "CE_SETUP"})

    async def broken_feed(now):
        raise RuntimeError("feed unavailable")

    p.feed.get_snapshot = broken_feed
    dashboard = await p.tick(MID_SESSION)
    assert p.decision.decision == "WAIT"
    assert p.decision.data_health == "STALE"
    assert p.features.data_health.checks["DATA_AGE"] == "STALE"
    assert dashboard.signal.decision == "WAIT"


async def test_tick_produces_full_dashboard_payload():
    p = _pipeline()
    payload = await p.tick(MID_SESSION)
    assert payload.market.spot is not None and payload.market.spot > 0
    assert payload.market.vwap is not None
    assert payload.market.atm is not None
    assert payload.pcr.total_pcr is not None
    assert payload.oi.put_oi and payload.oi.call_oi
    assert payload.option.atm_ce_ltp is not None and payload.option.atm_pe_ltp is not None
    assert payload.signal.decision in ("CE SETUP", "PE SETUP", "WAIT")
    assert payload.signal.ce_max_score == 100 and payload.signal.pe_max_score == 100
    assert payload.system.strategy_version == "1.0.0"
    assert payload.settings.min_score == 70


async def test_tick_queues_rows_for_all_data_tabs():
    p = _pipeline()
    await p.tick(MID_SESSION)
    assert p.writers.pending("RAW_DATA") == 7  # ATM +/- 3 strikes
    assert p.writers.pending("PCR_DATA") == 1
    assert p.writers.pending("SIGNAL") == 1  # WAIT decisions are logged too (§9)


async def test_time_filter_still_enforced_through_pipeline():
    p = _pipeline()
    payload = await p.tick(BEFORE_START)
    assert payload.signal.decision == "WAIT"
    assert any("TIME_FILTER_BEFORE_START" in r for r in payload.signal.reasons)


async def test_pcr_trend_becomes_available_over_successive_ticks():
    p = _pipeline()
    last = None
    for minute in range(0, 30, 5):
        last = await p.tick(MID_SESSION.replace(minute=minute))
    assert last is not None
    assert last.pcr.pcr_trend in ("UP", "DOWN", "FLAT")  # lookback satisfied
    assert last.pcr.pcr_change is not None


async def test_system_log_records_structured_events():
    p = _pipeline()
    await p.tick(MID_SESSION)
    events = {e.event for e in p.system_log.entries()}
    assert "SIGNAL_EVALUATED" in events
    entry = p.system_log.entries()[-1]
    assert entry.level in ("INFO", "WARN", "ERROR")
    assert entry.component and entry.message


async def test_feed_failure_is_contained_and_logged():
    p = _pipeline()
    await p.tick(MID_SESSION)

    async def boom(_now):
        raise RuntimeError("dhan timeout")

    p.feed.get_snapshot = boom  # simulate an API timeout mid-session
    payload = await p.tick(MID_SESSION)  # must NOT raise (§39)
    assert payload is not None
    assert any(e.event == "DHAN_CONNECTION" for e in p.system_log.entries())


async def test_sheets_disabled_without_credentials_and_flush_is_noop():
    p = _pipeline()
    await p.tick(MID_SESSION)
    status = p.sheets_status()
    assert status.configured is False and status.enabled is False
    assert await p.flush_sheets(force=True) == 0  # no credentials: nothing written, no crash
    await p.mirror_dashboard()  # no-op, must not raise


async def test_engines_contain_no_google_or_execution_imports():
    """Architecture separation (§14) + no execution path (§23), enforced."""
    import pathlib

    engine_dir = pathlib.Path(__file__).resolve().parent.parent / "engines"
    for path in engine_dir.glob("*.py"):
        text = path.read_text().lower()
        assert "googleapis" not in text and "sheets" not in text, f"{path.name} imports the Sheets layer"
        for banned in ("place_order", "placeorder", "buy_order", "sell_order", "modify_order", "cancel_order"):
            assert banned not in text, f"{path.name} contains an execution path: {banned}"


def test_dashboard_endpoint_returns_payload(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["signal"]["decision"] in ("CE SETUP", "PE SETUP", "WAIT")
    assert body["settings"]["index"] == "NIFTY"
    assert body["system"]["strategy_version"] == "1.0.0"
    assert body["data_health"]["overall"] in ("OK", "WARNING", "STALE", "ERROR", "DISABLED")


def test_system_log_endpoint(client):
    r = client.get("/system-log")
    assert r.status_code == 200
    assert isinstance(r.json()["entries"], list)


def test_sheets_status_endpoint(client):
    r = client.get("/sheets/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False  # PENDING until credentials are provided
    assert "last_error" in body and "rows_written" in body


def test_no_order_endpoints_exposed(client):
    """No execution surface exists anywhere in the API (§23)."""
    schema = client.get("http://localhost:8001/openapi.json").json()
    keys = list(schema["paths"].keys())
    paths = " ".join(keys).lower()
    for banned in ("order", "execute", "trade/place", "webhook"):
        assert banned not in paths, f"execution-like route exposed: {banned}"
    # Phase G added PAPER research positions. Any route mentioning a position
    # must live under /api/paper (paper/hypothetical records) and be GET-only —
    # a broker position route remains impossible.
    for path in keys:
        if "position" in path.lower():
            assert path.startswith("/api/paper/"), f"non-paper position route: {path}"
            assert set(schema["paths"][path]) <= {"get"}, path
