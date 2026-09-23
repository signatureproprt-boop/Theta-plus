"""Phase H — execution/risk layer tests (§52-§58).

EVERY test uses MockExecutionAdapter; no real order is ever placed. The matrix
proves broker calls = 0 for every unsafe state and exactly ONE submission when
all gates approve.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from engines.fixtures import SCENARIO_BUILDERS, default_config
from engines.signal_engine import evaluate, evaluate_and_apply
from engines.signal_memory import SignalMemory
from execution.adapter import ExecutionTimeout, MockExecutionAdapter
from execution.dhan_execution import DhanExecutionAdapter
from execution.gate import ExecutionFlags, can_execute_order
from execution.instruments import InstrumentMap, validate_instrument
from execution.reconciliation import PositionReconciliationService
from execution.risk_engine import RiskEngine, RiskState
from execution.service import ExecutionService, client_order_id_for
from lib.config import ConfigError, config_from_mapping
from lib.dates import IST
from models.execution_models import (
    APPROVED,
    BLOCK_COOLDOWN,
    BLOCK_DUPLICATE,
    BLOCK_INSTRUMENT,
    BLOCK_INVALID_SIGNAL,
    BLOCK_KILL_SWITCH,
    BLOCK_LIVE_DISABLED,
    BLOCK_MAX_DAILY_LOSS,
    BLOCK_MAX_POSITIONS,
    BLOCK_MAX_TRADES,
    BLOCK_MODE_NOT_LIVE,
    BLOCK_NOT_ARMED,
    BLOCK_PRICE_INVALID,
    BLOCK_QUANTITY,
    BLOCK_RECONCILIATION,
    BLOCK_SECURITY_ID,
    BLOCK_SESSION,
    BLOCK_SLIPPAGE,
    BLOCK_STALE_DATA,
    BLOCK_UNRECONCILED_POSITION,
    BROKER_STATUS_MAP,
    ORDER_FILLED,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    ORDER_SUBMITTED,
    ORDER_UNKNOWN,
    RECON_MISMATCH,
    RECON_OK,
    RECON_PENDING,
    BrokerPosition,
    OrderRequest,
)
from server import app

AT = datetime(2025, 1, 15, 12, 0, tzinfo=IST)
BASE = default_config()

INSTRUMENT_ROWS = [
    {"underlying": "NIFTY", "expiry": "2025-01-30", "strike": 25050, "option_type": "CALL",
     "security_id": "43492", "trading_symbol": "NIFTY 30 JAN 25050 CALL",
     "exchange_segment": "NSE_FNO", "lot_size": 1, "tradable": True},
    {"underlying": "NIFTY", "expiry": "2025-01-30", "strike": 25050, "option_type": "PUT",
     "security_id": "43493", "trading_symbol": "NIFTY 30 JAN 25050 PUT",
     "exchange_segment": "NSE_FNO", "lot_size": 1, "tradable": True},
]


def cfg(**overrides):
    data = BASE.model_dump()
    data["weights"] = BASE.weights.model_dump()
    data["invalidation"] = BASE.invalidation.model_dump()
    data.update(overrides)
    return config_from_mapping(data)


def live_cfg(**overrides):
    base = {"system_mode": "LIVE", "live_execution_enabled": True, "live_execution_armed": True,
            "risk_max_slippage_points": 5.0}
    base.update(overrides)
    return cfg(**base)


def decision_for(scenario: str = "strong_ce", at: datetime = AT, config=None):
    """Runs through evaluate_and_apply so the decision carries its post-apply
    state (ENTRY_CANDIDATE), exactly like the live pipeline produces it."""
    config = config or BASE
    features = SCENARIO_BUILDERS[scenario](at)
    return evaluate_and_apply(SignalMemory(), features, config, at), features


def service(monkeypatch, config, behaviour: str = "filled", armed_env: bool = True,
            reconciled: bool = True, adapter: MockExecutionAdapter | None = None) -> ExecutionService:
    monkeypatch.setenv("LIVE_EXECUTION_ENABLED", "true" if armed_env else "false")
    monkeypatch.setenv("LIVE_EXECUTION_ARMED", "true" if armed_env else "false")
    adapter = adapter or MockExecutionAdapter(behaviour=behaviour, fill_price=55.0)
    svc = ExecutionService(config, adapter=adapter, instrument_map=InstrumentMap(INSTRUMENT_ROWS))
    if reconciled:
        svc.reconciler.report = svc.reconciler.report.model_copy(
            update={"state": RECON_OK, "blocks_execution": False, "detail": "test reconciled"}
        )
    return svc


async def attempt(svc: ExecutionService, scenario: str = "strong_ce", at: datetime = AT,
                  signal_id: str = "sig-live-1", reference_price=None):
    decision, features = decision_for(scenario, at)
    return await svc.attempt_entry(decision, features, signal_id, now=at, reference_price=reference_price)


# =========================== API contract =================================
def test_dhan_execution_adapter_matches_the_verified_v2_contract():
    adapter = DhanExecutionAdapter(access_token="tok", client_id="CLIENT1")
    assert adapter.configured is True
    order = OrderRequest(client_order_id="PCR-abc", signal_id="s1", security_id="43492", quantity=1,
                         price=55.0)
    payload = order.to_dhan_payload("CLIENT1")
    assert set(payload) >= {
        "dhanClientId", "correlationId", "transactionType", "exchangeSegment", "productType",
        "orderType", "validity", "securityId", "quantity", "price", "afterMarketOrder",
    }
    assert payload["exchangeSegment"] == "NSE_FNO" and payload["productType"] == "INTRADAY"
    assert len(payload["correlationId"]) <= 30
    source = open("/app/backend/execution/dhan_execution.py").read()
    assert "https://api.dhan.co/v2" in source
    assert "access-token" in source
    assert "/orders/external/" in source  # correlation-id reconciliation path
    assert "/positions" in source


def test_broker_status_mapping_never_equates_submitted_with_filled():
    assert BROKER_STATUS_MAP["TRANSIT"] == ORDER_SUBMITTED
    assert BROKER_STATUS_MAP["TRADED"] == ORDER_FILLED
    assert BROKER_STATUS_MAP["PART_TRADED"] == ORDER_PARTIALLY_FILLED
    assert BROKER_STATUS_MAP["REJECTED"] == ORDER_REJECTED
    assert ORDER_SUBMITTED != ORDER_FILLED


def test_dhan_execution_adapter_is_inert_without_credentials():
    adapter = DhanExecutionAdapter(access_token="", client_id="")
    assert adapter.configured is False


def test_execution_adapter_redacts_credentials():
    adapter = DhanExecutionAdapter(access_token="SECRET-TOKEN-123", client_id="CLIENT-9")
    text = adapter.redact("boom SECRET-TOKEN-123 for CLIENT-9")
    assert "SECRET-TOKEN-123" not in text and "CLIENT-9" not in text


def test_market_data_and_execution_clients_stay_separate():
    exec_src = open("/app/backend/execution/dhan_execution.py").read()
    data_src = open("/app/backend/data/dhan_client.py").read()
    assert "option_chain" not in exec_src and "get_index_quote" not in exec_src
    for banned in ("/orders", "place_order", "submit_order"):
        assert banned not in data_src


# =========================== defaults =====================================
def test_fresh_deployment_defaults_are_safe():
    fresh = default_config()
    assert fresh.system_mode == "RESEARCH"
    assert fresh.live_execution_enabled is False
    assert fresh.live_execution_armed is False
    assert fresh.execution_adapter == "mock"


def test_live_mode_is_a_valid_configuration_but_blocked_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_EXECUTION_ARMED", raising=False)
    svc = ExecutionService(live_cfg(), adapter=MockExecutionAdapter())
    flags = svc.flags
    assert flags.mode == "LIVE"
    assert flags.live_execution_enabled is False  # env switch absent
    assert flags.live_execution_armed is False
    assert flags.control_enabled is False


def test_invalid_mode_is_rejected():
    with pytest.raises(ConfigError):
        cfg(system_mode="YOLO")


# =========================== safety matrix ================================
async def test_research_mode_makes_zero_broker_calls(monkeypatch):
    svc = service(monkeypatch, cfg(system_mode="RESEARCH"))
    verdict = await attempt(svc)
    assert verdict.approved is False and verdict.reason == BLOCK_MODE_NOT_LIVE
    assert svc.adapter.submit_count == 0


async def test_paper_mode_makes_zero_broker_calls(monkeypatch):
    svc = service(monkeypatch, cfg(system_mode="PAPER", paper_trading_enabled=True))
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_MODE_NOT_LIVE
    assert svc.adapter.submit_count == 0


async def test_live_but_disabled_makes_zero_broker_calls(monkeypatch):
    svc = service(monkeypatch, live_cfg(live_execution_enabled=False))
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_LIVE_DISABLED
    assert svc.adapter.submit_count == 0


async def test_live_but_disarmed_makes_zero_broker_calls(monkeypatch):
    svc = service(monkeypatch, live_cfg(live_execution_armed=False))
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_NOT_ARMED
    assert svc.adapter.submit_count == 0


async def test_env_switch_absent_blocks_even_when_config_arms(monkeypatch):
    svc = service(monkeypatch, live_cfg(), armed_env=False)
    verdict = await attempt(svc)
    assert verdict.reason in (BLOCK_LIVE_DISABLED, BLOCK_NOT_ARMED)
    assert svc.adapter.submit_count == 0


async def test_kill_switch_blocks_a_valid_ce_setup(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    svc.engage_kill_switch()
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_KILL_SWITCH
    assert svc.adapter.submit_count == 0


async def test_stale_data_makes_zero_broker_calls(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    verdict = await attempt(svc, scenario="stale")
    assert verdict.reason in (BLOCK_INVALID_SIGNAL, BLOCK_STALE_DATA)
    assert svc.adapter.submit_count == 0


async def test_wait_signal_never_reaches_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    verdict = await attempt(svc, scenario="weak")
    assert verdict.reason == BLOCK_INVALID_SIGNAL
    assert svc.adapter.submit_count == 0


async def test_outside_session_makes_zero_broker_calls(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    early = datetime(2025, 1, 15, 10, 0, tzinfo=IST)
    decision, features = decision_for("strong_ce", AT)  # a valid setup...
    verdict = await svc.attempt_entry(decision, features, "sig-early", now=early)  # ...at 10:00
    assert verdict.reason == BLOCK_SESSION
    assert svc.adapter.submit_count == 0


async def test_cooldown_blocks_execution(monkeypatch):
    """A decision suppressed by the Phase C cooldown can never execute."""
    svc = service(monkeypatch, live_cfg())
    memory = SignalMemory()
    features = SCENARIO_BUILDERS["strong_ce"](AT)
    evaluate_and_apply(memory, features, BASE, AT)  # emits the setup + starts cooldown
    later = AT + timedelta(minutes=2)
    cooled = evaluate_and_apply(memory, SCENARIO_BUILDERS["strong_ce"](later), BASE, later)
    assert any("SIGNAL_COOLDOWN" in r for r in cooled.reasons)
    verdict = await svc.attempt_entry(cooled, features, "sig-cool", now=later)
    assert verdict.reason == BLOCK_COOLDOWN
    assert svc.adapter.submit_count == 0


async def test_max_trades_per_day_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=1))
    svc.risk_state.trades_today = 1
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_MAX_TRADES
    assert svc.adapter.submit_count == 0


async def test_max_open_positions_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    svc.risk_state.open_positions = 1
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_MAX_POSITIONS
    assert svc.adapter.submit_count == 0


async def test_daily_loss_limit_locks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_daily_loss=100.0))
    svc.record_realized_pnl(-150.0)
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_MAX_DAILY_LOSS
    assert svc.adapter.submit_count == 0
    assert any(e.event == "LIVE_EXECUTION_LOCKED" for e in svc.entries())


async def test_invalid_quantity_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg(live_quantity=5, risk_max_quantity=1))
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_QUANTITY
    assert svc.adapter.submit_count == 0


async def test_missing_security_id_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    svc.instruments = InstrumentMap([])  # no mapping at all
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_SECURITY_ID
    assert svc.adapter.submit_count == 0


async def test_lot_size_violation_blocks_execution(monkeypatch):
    rows = [dict(INSTRUMENT_ROWS[0], lot_size=75)]
    svc = service(monkeypatch, live_cfg())
    svc.instruments = InstrumentMap(rows)
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_INSTRUMENT
    assert svc.adapter.submit_count == 0


async def test_non_tradable_instrument_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    svc.instruments = InstrumentMap([dict(INSTRUMENT_ROWS[0], tradable=False)])
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_INSTRUMENT
    assert svc.adapter.submit_count == 0


async def test_excessive_slippage_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_slippage_points=1.0))
    verdict = await attempt(svc, reference_price=10.0)  # option LTP is far from 10
    assert verdict.reason == BLOCK_SLIPPAGE
    assert svc.adapter.submit_count == 0


async def test_reconciliation_pending_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg(), reconciled=False)
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_RECONCILIATION
    assert svc.adapter.submit_count == 0


async def test_reconciliation_mismatch_blocks_execution(monkeypatch):
    svc = service(monkeypatch, live_cfg(), reconciled=False)
    svc.reconciler.report = svc.reconciler.report.model_copy(update={
        "state": RECON_MISMATCH,
        "unexpected_broker_positions": (BrokerPosition(security_id="43492", net_qty=75),),
        "detail": "broker has an unexpected open position",
    })
    verdict = await attempt(svc)
    assert verdict.reason == BLOCK_UNRECONCILED_POSITION
    assert svc.adapter.submit_count == 0


async def test_all_gates_approved_submits_exactly_once(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    verdict = await attempt(svc)
    assert verdict.approved is True and verdict.reason == APPROVED
    assert svc.adapter.submit_count == 1
    record = next(iter(svc.orders.values()))
    assert record.broker_order_id.startswith("MOCK-")
    assert record.status == ORDER_FILLED
    assert record.filled_quantity == record.requested_quantity
    assert record.average_fill_price == 55.0
    events = [e.event for e in svc.entries()]
    assert "EXECUTION_APPROVED" in events and "ORDER_SUBMITTED" in events


# =========================== idempotency / lifecycle ======================
def test_client_order_id_is_deterministic_and_short():
    a = client_order_id_for("sig-1")
    assert a == client_order_id_for("sig-1")
    assert a != client_order_id_for("sig-2")
    assert len(a) <= 30


async def test_duplicate_requests_submit_only_once(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=5))
    first = await attempt(svc, signal_id="sig-dup")
    second = await attempt(svc, signal_id="sig-dup")
    third = await attempt(svc, signal_id="sig-dup")
    assert first.approved is True
    assert second.reason == BLOCK_DUPLICATE and third.reason == BLOCK_DUPLICATE
    assert svc.adapter.submit_count == 1


async def test_timeout_yields_unknown_then_reconciles_without_resubmitting(monkeypatch):
    adapter = MockExecutionAdapter(behaviour="timeout")
    svc = service(monkeypatch, live_cfg(), adapter=adapter)
    verdict = await attempt(svc, signal_id="sig-timeout")
    assert verdict.approved is True
    assert adapter.submit_count == 1  # no blind retry
    record = next(iter(svc.orders.values()))
    assert record.status in (ORDER_UNKNOWN, "ACKNOWLEDGED")
    assert adapter.status_calls, "reconciliation query was not attempted"
    assert any(e.event == "ORDER_UNKNOWN" for e in svc.entries())


async def test_partial_fill_tracks_remaining_quantity(monkeypatch):
    adapter = MockExecutionAdapter(behaviour="partial", fill_quantity=1)
    svc = service(monkeypatch, live_cfg(live_quantity=3, risk_max_quantity=3), adapter=adapter)
    await attempt(svc, signal_id="sig-partial")
    record = next(iter(svc.orders.values()))
    assert record.status == ORDER_PARTIALLY_FILLED
    assert record.requested_quantity == 3
    assert record.filled_quantity == 1
    assert record.remaining_quantity == 2


async def test_rejected_order_is_recorded_not_filled(monkeypatch):
    adapter = MockExecutionAdapter(behaviour="rejected")
    svc = service(monkeypatch, live_cfg(), adapter=adapter)
    await attempt(svc, signal_id="sig-reject")
    record = next(iter(svc.orders.values()))
    assert record.status == ORDER_REJECTED
    assert record.filled_quantity == 0


async def test_refresh_order_uses_broker_as_authority(monkeypatch):
    adapter = MockExecutionAdapter(behaviour="acknowledged")
    svc = service(monkeypatch, live_cfg(), adapter=adapter)
    await attempt(svc, signal_id="sig-refresh")
    coid = client_order_id_for("sig-refresh")
    assert svc.orders[coid].status == "ACKNOWLEDGED"
    refreshed = await svc.refresh_order(coid)
    assert refreshed.broker_status == "PENDING"
    assert await svc.refresh_order("nope") is None


# =========================== reconciliation ===============================
async def test_reconciliation_detects_unexpected_broker_position():
    adapter = MockExecutionAdapter(positions=[BrokerPosition(security_id="43492", net_qty=75,
                                                             position_type="LONG")])
    recon = PositionReconciliationService(adapter)
    report = await recon.reconcile(internal_open=0)
    assert report.state == RECON_MISMATCH
    assert report.blocks_execution is True
    assert report.unexpected_broker_positions


async def test_reconciliation_ok_when_states_agree():
    recon = PositionReconciliationService(MockExecutionAdapter(positions=[]))
    report = await recon.reconcile(internal_open=0)
    assert report.state == RECON_OK and report.blocks_execution is False


async def test_startup_reconcile_blocks_when_broker_not_configured():
    svc = ExecutionService(live_cfg(), adapter=MockExecutionAdapter(is_configured=False))
    assert await svc.startup_reconcile() == RECON_PENDING
    assert svc.reconciler.report.blocks_execution is True


async def test_exit_requires_a_broker_confirmed_position(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    ok, reason = await svc.can_exit("43492")
    assert ok is False and reason == "NO_LIVE_POSITION"
    svc.adapter.positions = [BrokerPosition(security_id="43492", net_qty=1)]
    ok, reason = await svc.can_exit("43492")
    assert ok is True and reason == APPROVED


async def test_exit_blocked_when_reconciliation_not_ok(monkeypatch):
    svc = service(monkeypatch, live_cfg(), reconciled=False)
    svc.adapter.positions = [BrokerPosition(security_id="43492", net_qty=1)]
    ok, reason = await svc.can_exit("43492")
    assert ok is False and reason == BLOCK_RECONCILIATION


# =========================== risk engine units ============================
def test_risk_engine_is_deterministic_and_rejects_invalid_price():
    engine = RiskEngine(live_cfg())
    state = RiskState()
    assert engine.evaluate(state, 1, None, 50.0).approved is True
    assert engine.evaluate(state, 1, None, 0.0).reason == BLOCK_PRICE_INVALID
    assert engine.evaluate(state, 1, None, float("inf")).reason == BLOCK_PRICE_INVALID
    assert engine.evaluate(state, 1, None, float("nan")).reason == BLOCK_PRICE_INVALID
    assert engine.evaluate(state, 0, None, 50.0).reason == BLOCK_QUANTITY


def test_risk_engine_disabled_fails_closed():
    engine = RiskEngine(live_cfg(risk_enabled=False))
    assert engine.evaluate(RiskState(), 1, None, 50.0).approved is False


def test_risk_engine_never_increases_quantity():
    engine = RiskEngine(live_cfg(risk_max_quantity=1))
    decision = engine.evaluate(RiskState(), 2, None, 50.0)
    assert decision.approved is False and decision.reason == BLOCK_QUANTITY
    source = open("/app/backend/execution/risk_engine.py").read().lower()
    for banned in ("sklearn", "model.predict", "numpy.random", "available_balance",
                   "funds", "optimi"):
        assert banned not in source


def test_instrument_map_never_guesses(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps(INSTRUMENT_ROWS))
    mapping = InstrumentMap(json.loads(path.read_text()))
    assert mapping.resolve("NIFTY", 25050, "CE").security_id == "43492"
    assert mapping.resolve("NIFTY", 25050, "PE").security_id == "43493"
    assert mapping.resolve("NIFTY", 99999, "CE") is None
    assert mapping.resolve("NIFTY", None, "CE") is None
    ok, detail = validate_instrument(None, 1, 50.0)
    assert ok is False and "mapping" in detail


def test_order_request_rejects_invalid_values():
    with pytest.raises(Exception):
        OrderRequest(client_order_id="a", signal_id="s", security_id="", quantity=1)
    with pytest.raises(Exception):
        OrderRequest(client_order_id="a", signal_id="s", security_id="1", quantity=0)
    with pytest.raises(Exception):
        OrderRequest(client_order_id="a", signal_id="s", security_id="1", quantity=1,
                     transaction_type="HOLD")


# =========================== gate purity ==================================
def test_gate_is_the_only_approval_path_and_has_no_adapter():
    gate_src = open("/app/backend/execution/gate.py").read()
    assert "submit_order" not in gate_src  # the gate decides, it never submits
    service_src = open("/app/backend/execution/service.py").read()
    assert service_src.count("submit_order(") <= 2  # the single submission site
    assert "can_execute_order" in service_src


def test_no_other_module_holds_an_execution_adapter():
    import pathlib

    allowed = {
        "execution/adapter.py", "execution/dhan_execution.py", "execution/service.py",
        "execution/reconciliation.py", "execution/gate.py", "execution/instruments.py",
        "pipeline.py", "routers/execution_router.py",
    }
    root = pathlib.Path("/app/backend")
    for file in root.rglob("*.py"):
        rel = str(file.relative_to(root))
        if rel.startswith("tests/") or rel in allowed:
            continue
        text = file.read_text()
        assert "submit_order" not in text, rel
        assert "DhanExecutionAdapter" not in text, rel


def test_strategy_engines_contain_no_execution_import():
    import pathlib

    for file in pathlib.Path("/app/backend/engines").rglob("*.py"):
        low = file.read_text().lower()
        for banned in ("execution", "dhan", "submit_order", "place_order", "adapter"):
            assert banned not in low, (file.name, banned)


def test_no_hidden_order_symbols_outside_the_execution_layer():
    import pathlib

    banned = ("place_order", "buy_order", "sell_order", "modify_order", "cancel_order")
    root = pathlib.Path("/app/backend")
    for file in root.rglob("*.py"):
        rel = str(file.relative_to(root))
        if rel.startswith(("execution/", "tests/")):
            continue
        low = file.read_text().lower()
        for word in banned:
            assert word not in low, (rel, word)


# =========================== API ==========================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_execution_status_endpoint_reports_safe_defaults(client):
    res = client.get("/api/execution/status")
    assert res.status_code == 200
    body = res.json()
    assert body["system_mode"] == "RESEARCH"
    assert body["live_execution_enabled"] is False
    assert body["live_execution_armed"] is False
    assert body["control_enabled"] is False
    assert body["broker_verified"] is False
    assert body["reconciliation"] in (RECON_PENDING, RECON_OK, RECON_MISMATCH)
    assert body["adapter"] in ("mock", "dhan")
    assert body["confirmation_required"]
    raw = res.text
    for banned in ("DHAN_ACCESS_TOKEN", "access-token", "dhanClientId"):
        assert banned not in raw


def test_execution_log_and_reconciliation_endpoints(client):
    assert client.get("/api/execution/log").status_code == 200
    assert client.get("/api/execution/submissions").status_code == 200
    recon = client.get("/api/execution/reconciliation")
    assert recon.status_code == 200
    assert recon.json()["state"] in (RECON_PENDING, RECON_OK, RECON_MISMATCH)


def test_no_arming_or_order_endpoint_exists(client):
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        low = path.lower()
        assert "order" not in low, path
        for banned in ("arm", "enable", "execute", "submit", "webhook"):
            assert banned not in low, path
        if low.startswith("/api/execution"):
            assert set(methods) <= {"get"}, (path, list(methods))


def test_execution_api_is_readonly(client):
    assert client.post("/api/execution/status").status_code == 405
    assert client.get("/api/execution/does-not-exist").status_code == 404
