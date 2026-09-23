"""Phase G — alert engine tests (§30/§31).

Covers configured/missing/failing Telegram, dedupe, CE/PE/invalidation alerts,
WAIT exclusion, credential redaction and failure isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from alerts.formatter import alert_type_for, format_alert
from alerts.service import AlertService
from alerts.telegram import TelegramProvider
from chart.markers import link_invalidations, marker_from_decision
from engines.fixtures import SCENARIO_BUILDERS
from engines.signal_engine import evaluate, evaluate_and_apply
from engines.signal_memory import SignalMemory
from lib.config import get_config
from lib.dates import IST
from models.alert_models import (
    STATUS_DISABLED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SENT,
)

AT = datetime(2025, 1, 15, 12, 0, tzinfo=IST)
TOKEN = "123456789:AAtestSECRETtokenVALUE"
CHAT = "-1009998887776"


def marker(scenario: str = "strong_ce", at: datetime = AT):
    config = get_config()
    features = SCENARIO_BUILDERS[scenario](at)
    decision = evaluate(features, config, SignalMemory().snapshot(), at)
    return marker_from_decision(decision, features), decision


class FakeProvider:
    """Stand-in channel proving the interface is provider-independent."""

    name = "fake"

    def __init__(self, configured: bool = True, fail: bool = False) -> None:
        self._configured = configured
        self.fail = fail
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def redact(self, text: str) -> str:
        return text.replace(TOKEN, "***REDACTED***")

    async def send(self, message: str) -> None:
        if self.fail:
            raise RuntimeError(f"upstream 500 for token {TOKEN}")
        self.sent.append(message)


# --- alert types ----------------------------------------------------------
def test_alert_types_cover_setups_and_invalidation_only():
    assert alert_type_for("CE_SETUP") == "CE_SETUP"
    assert alert_type_for("PE_SETUP") == "PE_SETUP"
    assert alert_type_for("CE_INVALIDATED") == "INVALIDATED"
    assert alert_type_for("PE_INVALIDATED") == "INVALIDATED"
    assert alert_type_for("WAIT") is None


@pytest.mark.asyncio
async def test_wait_decisions_are_never_alerted():
    provider = FakeProvider()
    service = AlertService(provider=provider)
    m, decision = marker("weak")
    assert decision.decision == "WAIT"
    assert await service.dispatch(m) is None
    assert provider.sent == []
    assert service.records() == []


@pytest.mark.asyncio
async def test_ce_alert_is_sent_with_deterministic_backend_values():
    provider = FakeProvider()
    service = AlertService(provider=provider, mode="PAPER")
    m, decision = marker("strong_ce")
    record = await service.dispatch(m)
    assert record is not None and record.status == STATUS_SENT
    assert record.alert_type == "CE_SETUP" and record.signal_id == m.signal_id
    body = provider.sent[0]
    assert "NIFTY CE SETUP" in body
    assert f"{m.score}/{m.max_score}" in body
    assert f"{decision.pcr:.2f}" in body
    assert "Mode: PAPER" in body
    assert "LIVE • SIMULATED DATA" in body
    assert "no execution path exists" in body


@pytest.mark.asyncio
async def test_pe_alert_is_sent():
    provider = FakeProvider()
    service = AlertService(provider=provider)
    m, decision = marker("strong_pe")
    assert decision.decision == "PE_SETUP"
    record = await service.dispatch(m)
    assert record.status == STATUS_SENT and record.alert_type == "PE_SETUP"
    assert "NIFTY PE SETUP" in provider.sent[0]


def test_alert_text_never_uses_probability_language():
    """No percentage/guarantee claims; "probability" may appear only as a denial."""
    import re

    m, _ = marker("strong_ce")
    text = format_alert(m, "PAPER").lower()
    for banned in ("guaranteed", "sure shot", "profit confirmed", "accuracy", "win rate"):
        assert banned not in text, banned
    assert not re.search(r"\d\s*%", text), "no percentage claim may appear"
    for match in re.finditer(r"probability", text):
        prefix = text[max(0, match.start() - 10):match.start()]
        assert "not " in prefix, f"'probability' used as a claim near: {prefix!r}"


@pytest.mark.asyncio
async def test_invalidation_alert_keeps_the_original_signal_id():
    config = get_config()
    memory = SignalMemory()
    t0 = AT
    setup = evaluate_and_apply(memory, SCENARIO_BUILDERS["strong_ce"](t0), config, t0)
    for minutes in (10, 20):
        at = t0 + timedelta(minutes=minutes)
        evaluate_and_apply(memory, SCENARIO_BUILDERS["strong_ce"](at), config, at)
    t1 = t0 + timedelta(minutes=21)
    inval = evaluate_and_apply(memory, SCENARIO_BUILDERS["ce_below_vwap"](t1), config, t1)
    assert inval.decision == "CE_INVALIDATED"

    markers = link_invalidations([marker_from_decision(setup, None), marker_from_decision(inval, None)])
    provider = FakeProvider()
    service = AlertService(provider=provider)
    await service.dispatch(markers[0])
    record = await service.dispatch(markers[1])
    assert record.alert_type == "INVALIDATED"
    body = provider.sent[1]
    assert "CE INVALIDATED" in body
    assert markers[0].signal_id in body  # original signal retained
    assert "nothing was deleted" in body


# --- dedupe / failure / config -------------------------------------------
@pytest.mark.asyncio
async def test_same_signal_twice_produces_one_alert():
    provider = FakeProvider()
    service = AlertService(provider=provider)
    m, _ = marker("strong_ce")
    first = await service.dispatch(m)
    second = await service.dispatch(m)
    assert first.status == STATUS_SENT
    assert second.status == STATUS_DUPLICATE
    assert len(provider.sent) == 1
    assert service.status().suppressed_duplicates == 1


@pytest.mark.asyncio
async def test_missing_credentials_record_not_configured_and_do_not_raise():
    provider = FakeProvider(configured=False)
    service = AlertService(provider=provider)
    m, _ = marker("strong_ce")
    record = await service.dispatch(m)
    assert record.status == STATUS_NOT_CONFIGURED
    assert provider.sent == []
    status = service.status()
    assert status.configured is False and status.delivery == "PENDING"


@pytest.mark.asyncio
async def test_provider_failure_is_contained_and_redacted():
    provider = FakeProvider(fail=True)
    logs: list[tuple] = []
    service = AlertService(provider=provider, log_sink=lambda *a: logs.append(a))
    m, _ = marker("strong_ce")
    record = await service.dispatch(m)  # must not raise
    assert record.status == STATUS_FAILED
    assert TOKEN not in record.error and "***REDACTED***" in record.error
    assert any(entry[2] == "ALERT_FAILED" for entry in logs)
    assert service.status().failed == 1


@pytest.mark.asyncio
async def test_alerts_disabled_records_disabled():
    provider = FakeProvider()
    service = AlertService(provider=provider, enabled=False)
    m, _ = marker("strong_ce")
    assert (await service.dispatch(m)).status == STATUS_DISABLED
    assert provider.sent == []


# --- telegram provider ----------------------------------------------------
def test_telegram_not_configured_without_env():
    assert TelegramProvider(token="", chat_id="").configured is False


def test_telegram_configured_with_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert TelegramProvider().configured is True


def test_telegram_redacts_token_and_chat_id():
    provider = TelegramProvider(token=TOKEN, chat_id=CHAT)
    text = f"failed for {TOKEN} in chat {CHAT}"
    redacted = provider.redact(text)
    assert TOKEN not in redacted and CHAT not in redacted
    assert "AAtestSECRETtokenVALUE" not in redacted


@pytest.mark.asyncio
async def test_telegram_send_without_credentials_raises_not_configured():
    provider = TelegramProvider(token="", chat_id="")
    with pytest.raises(RuntimeError) as exc:
        await provider.send("hello")
    assert "NOT_CONFIGURED" in str(exc.value)


def test_alert_records_never_contain_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    service = AlertService()
    dumped = service.status().model_dump_json()
    assert TOKEN not in dumped and CHAT not in dumped
    source = open("/app/backend/alerts/telegram.py").read()
    assert TOKEN not in source
    assert "os.environ" in source  # credentials only ever come from the env
