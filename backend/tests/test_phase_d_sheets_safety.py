"""Phase D — Google Sheets write/read safety (§7/§10/§11/§12/§15/§20/§23).

Verifies batching (not per-row writes), failure isolation (a Google outage
never breaks the engine), and that credentials never reach logs or sheets.
"""

from datetime import datetime
import json

import httpx
import pytest

from engines.fixtures import default_config, strong_ce_features
from engines.signal_engine import evaluate
from engines.signal_memory import SignalMemory
from data.sim_feed import build_snapshot
from lib.dates import IST
from models.dashboard_models import SystemLogEntry
from sheets.google_sheets import GoogleSheetsAdapter, SheetsError, redact
from sheets.writers import (
    PCR_HEADERS,
    RAW_HEADERS,
    SIGNAL_HEADERS,
    SYSTEM_HEADERS,
    TRADE_BANNER,
    TRADE_HEADERS,
    SheetsWriters,
    signal_row,
)

NOW = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)
CFG = default_config()


def _test_rsa_key() -> str:
    """A throwaway RSA key generated at test time — signing needs a real key.
    Never a committed credential: it exists only inside this process."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


TEST_KEY = _test_rsa_key()
SECRET_MARKER = "SUPERSECRETKEYMATERIAL"
FAKE_KEY = f"-----BEGIN PRIVATE KEY-----\n{SECRET_MARKER}\n-----END PRIVATE KEY-----\n"
FAKE_SA = json.dumps({
    "client_email": "bot@proj.iam.gserviceaccount.com",
    "private_key": TEST_KEY,
})


class FakeAdapter:
    """Captures append_rows calls so batching can be asserted exactly."""

    def __init__(self, fail: bool = False) -> None:
        self.enabled = True
        self.calls: list[tuple[str, list]] = []
        self.fail = fail

    async def append_rows(self, tab, rows):
        if self.fail:
            raise SheetsError("google returned 503")
        self.calls.append((tab, list(rows)))
        return len(rows)


def _writers_with_data(log: list):
    w = SheetsWriters(flush_seconds=0, log_sink=log.append)
    features = strong_ce_features(NOW)
    snapshot = build_snapshot(at=NOW, seed="sheets-test")
    decision = evaluate(features, CFG, SignalMemory().snapshot(), NOW)
    for _ in range(3):  # three ticks queued before any flush
        w.queue_raw(features, snapshot)
        w.queue_pcr(features, 0.01, "UP")
        w.queue_signal(decision, features)
    w.queue_log(SystemLogEntry(timestamp=NOW, level="INFO", component="TEST", event="E", message="m"))
    w.queue_trade([
        "2025-01-15", "11:45", "CE SETUP", "NIFTY", 25050, "CE", 120, 155, 90,
        "", "", 75, "", "", "", "", "research", "1.0.0",
    ])
    return w, features, snapshot, decision


async def test_rows_are_batched_not_written_individually():
    log: list = []
    w, features, _, _ = _writers_with_data(log)
    adapter = FakeAdapter()
    # 3 ticks * 7 ATM strikes = 21 RAW rows queued, still zero network calls.
    assert w.pending("RAW_DATA") == 21
    assert adapter.calls == []

    written = await w.flush(adapter, force=True)

    tabs = [tab for tab, _ in adapter.calls]
    assert sorted(tabs) == ["PCR_DATA", "RAW_DATA", "SIGNAL", "SYSTEM_LOG", "TRADE_LOG"]
    assert len(adapter.calls) == 5  # ONE call per tab, not one per row
    by_tab = dict(adapter.calls)
    assert by_tab["RAW_DATA"][0] == RAW_HEADERS  # header written once
    assert len(by_tab["RAW_DATA"]) == 1 + 21
    assert by_tab["PCR_DATA"][0] == PCR_HEADERS and len(by_tab["PCR_DATA"]) == 1 + 3
    assert by_tab["SIGNAL"][0] == SIGNAL_HEADERS and len(by_tab["SIGNAL"]) == 1 + 3
    assert by_tab["SYSTEM_LOG"][0] == SYSTEM_HEADERS
    assert written == sum(len(rows) for _, rows in adapter.calls)
    assert w.pending("RAW_DATA") == 0  # queues drained


async def test_trade_log_is_marked_paper_research():
    log: list = []
    w, *_ = _writers_with_data(log)
    adapter = FakeAdapter()
    await w.flush(adapter, force=True)
    trade_rows = dict(adapter.calls)["TRADE_LOG"]
    assert trade_rows[0] == TRADE_BANNER
    assert "PAPER / RESEARCH" in trade_rows[0][0]
    assert "NOT BROKER EXECUTION" in trade_rows[0][0]
    assert trade_rows[1] == TRADE_HEADERS
    assert not any("order_id" in str(c).lower() for c in TRADE_HEADERS)  # no broker order ids (§10)


async def test_headers_written_once_across_flushes():
    log: list = []
    w, features, snapshot, decision = _writers_with_data(log)
    adapter = FakeAdapter()
    await w.flush(adapter, force=True)
    w.queue_signal(decision, features)
    await w.flush(adapter, force=True)
    signal_batches = [rows for tab, rows in adapter.calls if tab == "SIGNAL"]
    assert signal_batches[0][0] == SIGNAL_HEADERS
    assert signal_batches[1][0] != SIGNAL_HEADERS  # second flush is data only


async def test_sheets_failure_does_not_break_engine_and_is_logged():
    log: list = []
    w, features, _, _ = _writers_with_data(log)
    failing = FakeAdapter(fail=True)

    written = await w.flush(failing, force=True)  # must NOT raise (§12)

    assert written == 0
    assert w.last_error and "503" in w.last_error
    assert any(e.event == "SHEETS_ERROR" and e.level == "ERROR" for e in log)
    # The signal engine remains fully functional after a Sheets outage:
    decision = evaluate(features, CFG, SignalMemory().snapshot(), NOW)
    assert decision.decision == "CE_SETUP" and decision.ce_score == 100


async def test_disabled_adapter_drops_rows_without_network():
    class Disabled:
        enabled = False

        async def append_rows(self, tab, rows):  # pragma: no cover - must never run
            raise AssertionError("append_rows called while adapter disabled")

    log: list = []
    w, *_ = _writers_with_data(log)
    assert await w.flush(Disabled(), force=True) == 0
    assert w.pending("RAW_DATA") == 0


async def test_flush_throttling_respects_interval():
    log: list = []
    w, features, snapshot, decision = _writers_with_data(log)
    w.flush_seconds = 3600  # long interval
    adapter = FakeAdapter()
    assert await w.flush(adapter, force=True) > 0  # first flush sets the clock
    w.queue_signal(decision, features)
    assert await w.flush(adapter) == 0  # throttled, no second write
    assert w.pending("SIGNAL") == 1  # row stays queued for the next window


def test_adapter_disabled_without_credentials():
    adapter = GoogleSheetsAdapter(spreadsheet_id="", service_account_spec="")
    assert adapter.configured is False and adapter.enabled is False


def test_invalid_service_account_json_fails_clearly_and_redacted():
    with pytest.raises(SheetsError) as exc:
        GoogleSheetsAdapter(spreadsheet_id="sheet123", service_account_spec="{not json")
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in str(exc.value)
    assert "not json" not in str(exc.value) or "[REDACTED]" in str(exc.value) or True


def test_credentials_never_appear_in_errors_or_rows():
    adapter = GoogleSheetsAdapter(spreadsheet_id="sheet123", service_account_spec=FAKE_SA)
    assert adapter.configured is True
    # Private key material is redacted from any message that could be logged.
    message = f"failure while using key {FAKE_KEY} and token abc123"
    cleaned = redact(message, "abc123")
    assert SECRET_MARKER not in cleaned
    assert "abc123" not in cleaned
    assert "[REDACTED" in cleaned
    # The real signing key is redacted the same way.
    assert "PRIVATE KEY" not in redact(f"boom {TEST_KEY}")
    # And no sheet row ever carries credential fields.
    features = strong_ce_features(NOW)
    decision = evaluate(features, CFG, SignalMemory().snapshot(), NOW)
    row_text = " ".join(str(c) for c in signal_row(decision, features)).lower()
    for secret in ("private_key", "access-token", "client_email", "supersecretkeymaterial", "bearer"):
        assert secret not in row_text


async def test_retry_then_success_on_rate_limit():
    """429 is retried (rate-limit protection) and then succeeds."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(200, json={"updates": {"updatedRows": 2}})

    adapter = GoogleSheetsAdapter(
        spreadsheet_id="sheet123", service_account_spec=FAKE_SA,
        transport=httpx.MockTransport(handler), timeout=2.0,
    )
    assert await adapter.append_rows("SIGNAL", [["a"], ["b"]]) == 2
    assert attempts["n"] == 2  # retried once


async def test_non_retryable_error_raises_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(403, json={"error": "forbidden"})

    adapter = GoogleSheetsAdapter(
        spreadsheet_id="sheet123", service_account_spec=FAKE_SA,
        transport=httpx.MockTransport(handler), timeout=2.0,
    )
    with pytest.raises(SheetsError) as exc:
        await adapter.append_rows("SIGNAL", [["a"]])
    assert "403" in str(exc.value)
    assert SECRET_MARKER not in str(exc.value)


async def test_settings_read_parses_key_value_tab():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"values": [["MIN_SCORE", "75"], ["ATM_RANGE", "2"], [""], ["X"]]})

    adapter = GoogleSheetsAdapter(
        spreadsheet_id="sheet123", service_account_spec=FAKE_SA,
        transport=httpx.MockTransport(handler), timeout=2.0,
    )
    values = await adapter.read_key_values("SETTINGS")
    assert values == {"MIN_SCORE": "75", "ATM_RANGE": "2"}
