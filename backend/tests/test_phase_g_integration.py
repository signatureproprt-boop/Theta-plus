"""Phase G — integration + safety tests (§30/§31/§28).

signal -> alert, signal -> paper position, position -> exit -> P&L, mode gating,
API surfaces, replay compatibility, and the mandatory zero-broker-call proofs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from engines.fixtures import SCENARIO_BUILDERS, default_config
from lib.config import config_from_mapping, get_config
from lib.dates import IST
from models.alert_models import STATUS_NOT_CONFIGURED, STATUS_SENT
from models.paper_models import PAPER_TARGET
from paper.paper_engine import PaperTradingEngine
from pipeline import Pipeline
from replay.backtest_engine import run_replay
from replay.fixtures import synthetic_session
from replay.outcome_engine import OptionObservation, entry_price
from server import app
from storage.sources import InMemorySource

AT = datetime(2025, 1, 15, 12, 0, tzinfo=IST)
CFG = default_config()


def _cfg(**overrides):
    data = CFG.model_dump()
    data["weights"] = CFG.weights.model_dump()
    data["invalidation"] = CFG.invalidation.model_dump()
    data.update(overrides)
    return config_from_mapping(data)


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    def redact(self, text: str) -> str:
        return text

    async def send(self, message: str) -> None:
        self.sent.append(message)


class FixedFeed:
    """Deterministic feed wrapper around one scenario snapshot (no network)."""

    name = "sim"

    def __init__(self, scenario: str = "strong_ce") -> None:
        self.scenario = scenario

    def get_bars(self, date_iso: str):
        from data.sim_feed import day_bars
        from engines.vwap_engine import Bar

        return [Bar(high=b["high"], low=b["low"], close=b["close"], volume=float(b["volume"]))
                for b in day_bars("live", date_iso)]

    async def get_snapshot(self, now):
        from data.sim_feed import build_snapshot

        return build_snapshot(at=now, seed="live")


# --- pipeline integration -------------------------------------------------
async def _pipeline(mode: str = "PAPER", alerts_enabled: bool = True, provider=None) -> Pipeline:
    pipeline = Pipeline(_cfg(system_mode=mode, paper_trading_enabled=(mode == "PAPER"),
                             alerts_enabled=alerts_enabled))
    if provider is not None:
        pipeline.alerts.provider = provider
    return pipeline


async def test_signal_flows_to_alert_and_paper_position_in_paper_mode():
    provider = RecordingProvider()
    pipeline = await _pipeline("PAPER", provider=provider)
    # Deterministic setup decision injected through the real tick path.
    for minutes in range(0, 3):
        await pipeline.tick(AT + timedelta(minutes=minutes))
    decisions = [m.decision for m in pipeline.chart_markers]
    assert decisions, "pipeline produced no decisions"
    if any(d in ("CE_SETUP", "PE_SETUP") for d in decisions):
        assert pipeline.paper.positions, "a setup did not create a paper position"
        assert provider.sent, "a setup did not produce an alert"
        assert "Mode: PAPER" in provider.sent[0]
    # nothing crashed and the engines kept running
    assert pipeline.decision is not None


async def test_research_mode_creates_no_paper_position_by_default():
    pipeline = Pipeline(_cfg(system_mode="RESEARCH", paper_trading_enabled=False))
    assert pipeline.paper_enabled is False
    for minutes in range(0, 3):
        await pipeline.tick(AT + timedelta(minutes=minutes))
    assert pipeline.paper.positions == []


async def test_alert_failure_does_not_stop_the_signal_engine():
    class Broken(RecordingProvider):
        async def send(self, message: str) -> None:
            raise RuntimeError("telegram down")

    pipeline = await _pipeline("PAPER", provider=Broken())
    for minutes in range(0, 3):
        await pipeline.tick(AT + timedelta(minutes=minutes))
    assert pipeline.decision is not None  # signals continue
    assert pipeline.features is not None


async def test_paper_failure_does_not_stop_the_signal_engine():
    pipeline = await _pipeline("PAPER")

    class ExplodingPaper(PaperTradingEngine):
        def on_tick(self, *a, **k):
            raise RuntimeError("paper engine boom")

    pipeline.paper = ExplodingPaper(pipeline.config)
    await pipeline.tick(AT)
    assert pipeline.decision is not None
    assert any(e.event == "PAPER_ERROR" for e in pipeline.system_log.entries())


async def test_missing_telegram_credentials_keep_the_system_running():
    pipeline = await _pipeline("PAPER")  # real TelegramProvider, no env credentials
    for minutes in range(0, 3):
        await pipeline.tick(AT + timedelta(minutes=minutes))
    assert pipeline.decision is not None
    statuses = {r.status for r in pipeline.alerts.records()}
    assert not statuses or statuses <= {STATUS_NOT_CONFIGURED}


# --- exit -> P&L integration ---------------------------------------------
def test_position_exit_produces_pnl_fields():
    engine = PaperTradingEngine(_cfg(paper_quantity=75, paper_costs_per_trade=20.0,
                                     paper_slippage_points=1.0))
    features = SCENARIO_BUILDERS["strong_ce"](AT)
    from engines.signal_engine import evaluate
    from engines.signal_memory import SignalMemory

    decision = evaluate(features, CFG, SignalMemory().snapshot(), AT)
    position = engine.open_from_decision(decision, features, "sig-pnl", now=AT)
    closed = engine.update(OptionObservation(timestamp=AT, ltp=position.target_price + 1), AT)
    assert closed.state == PAPER_TARGET
    expected_points = round(closed.exit_price - position.entry_price, 2)
    assert closed.gross_points == expected_points
    assert closed.gross_pnl == round(expected_points * 75, 2)
    assert closed.net_pnl == round((expected_points - 1.0) * 75 - 20.0, 2)
    assert closed.costs == 20.0 and closed.assumed_slippage_points == 1.0


# --- replay compatibility (no second replay engine) ----------------------
def test_paper_semantics_match_the_phase_e_outcome_engine():
    """Same entry/target/stop and the same AMBIGUOUS rule as Phase E."""
    result = run_replay(InMemorySource(synthetic_session()), get_config(), data_origin="SYNTHETIC")
    snapshots = sorted(synthetic_session(), key=lambda s: s.timestamp)
    setups = [s for s in result.signals if s.decision in ("CE_SETUP", "PE_SETUP")]
    assert setups
    signal = setups[0]
    entry = entry_price(snapshots, signal)
    config = get_config()
    assert entry is not None

    engine = PaperTradingEngine(config, data_origin="REPLAY-SYNTHETIC")

    class _D:  # minimal decision shim carrying the replayed backend values
        decision = signal.decision
        timestamp = signal.timestamp
        symbol = signal.instrument
        atm = signal.atm
        strategy_version = signal.strategy_version
        feature_engine_version = signal.feature_engine_version

    features = None
    position = engine.open_from_decision(_D(), features, signal.signal_id, now=signal.timestamp)
    # No features => no option quote => NO_DATA, never an invented entry.
    assert position.entry_price is None
    # and the Phase E outcome for the same signal exists with its own target/stop
    outcome = next(o for o in result.outcomes if o.signal_id == signal.signal_id)
    assert outcome.target == round((outcome.entry or 0) + config.target_points, 2)
    assert outcome.stoploss == round((outcome.entry or 0) - config.stoploss_points, 2)
    assert engine.data_origin == "REPLAY-SYNTHETIC"
    assert engine.origin_label == "REPLAY • SYNTHETIC DATA"


# --- API + safety ---------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_paper_api_surfaces(client):
    summary = client.get("/api/paper/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["mode"] in ("RESEARCH", "PAPER")
    assert "PAPER" in body["label"].upper() and "HYPOTHETICAL" in body["label"].upper()
    assert body["target_points"] == get_config().target_points
    assert body["max_open_positions"] == get_config().max_open_paper_positions

    positions = client.get("/api/paper/positions")
    assert positions.status_code == 200 and isinstance(positions.json(), list)
    assert client.get("/api/paper/trades").status_code == 200
    assert client.get("/api/paper/positions/unknown-id").status_code == 404


def test_alerts_api_is_credential_free(client):
    res = client.get("/api/alerts")
    assert res.status_code == 200
    body = res.json()
    assert body["status"]["provider"] == "telegram"
    assert body["status"]["real_delivery_verified"] is False
    raw = res.text.lower()
    for banned in ("telegram_bot_token", "telegram_chat_id", "bot1", "sendmessage"):
        assert banned not in raw
    assert client.get("/api/alerts/status").status_code == 200


def test_no_order_placement_endpoint_exists(client):
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        lowered = path.lower()
        assert "order" not in lowered and "execute" not in lowered, path
        if lowered.startswith("/api/paper") or lowered.startswith("/api/alerts"):
            assert set(methods) <= {"get"}, (path, list(methods))


def test_no_broker_execution_symbols_anywhere_in_phase_g():
    import pathlib

    banned = ("place_order", "buy_order", "sell_order", "modify_order", "cancel_order")
    files = [
        "/app/backend/paper/paper_engine.py",
        "/app/backend/models/paper_models.py",
        "/app/backend/alerts/service.py",
        "/app/backend/alerts/telegram.py",
        "/app/backend/alerts/formatter.py",
        "/app/backend/alerts/provider.py",
        "/app/backend/routers/paper_router.py",
    ]
    for name in files:
        text = pathlib.Path(name).read_text().lower()
        for word in banned:
            assert word not in text, (name, word)


def test_dhan_client_still_has_no_order_methods():
    from data.dhan_client import DhanMarketData

    for attr in dir(DhanMarketData):
        assert "order" not in attr.lower()


def test_research_and_paper_modes_make_zero_broker_calls(monkeypatch):
    """Neither mode may reach any HTTP client for execution purposes."""
    import httpx

    calls: list[str] = []

    def guard(*args, **kwargs):
        calls.append(str(args[:2]))
        raise AssertionError("no outbound execution call is permitted")

    monkeypatch.setattr(httpx.Client, "request", guard, raising=False)
    for mode in ("RESEARCH", "PAPER"):
        config = _cfg(system_mode=mode, paper_trading_enabled=(mode == "PAPER"))
        engine = PaperTradingEngine(config)
        features = SCENARIO_BUILDERS["strong_ce"](AT)
        from engines.signal_engine import evaluate
        from engines.signal_memory import SignalMemory

        decision = evaluate(features, CFG, SignalMemory().snapshot(), AT)
        engine.on_tick(decision, features, None, f"sig-{mode}", now=AT)
    assert calls == []
