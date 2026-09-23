"""Phase G — PAPER trading engine tests (§30).

Entry (CE/PE), target, stoploss, invalidation, same-observation ambiguity,
missing data, duplicate signal, max open positions, and P&L. ZERO broker calls.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from engines.fixtures import SCENARIO_BUILDERS, default_config
from engines.signal_engine import evaluate, evaluate_and_apply
from engines.signal_memory import SignalMemory
from lib.config import config_from_mapping
from lib.dates import IST
from models.paper_models import (
    PAPER_AMBIGUOUS,
    PAPER_EXPIRED,
    PAPER_INVALIDATED,
    PAPER_NO_DATA,
    PAPER_OPEN,
    PAPER_STOPLOSS,
    PAPER_TARGET,
)
from paper.paper_engine import PaperTradingEngine, observation_for
from replay.outcome_engine import OptionObservation

AT = datetime(2025, 1, 15, 12, 0, tzinfo=IST)
CFG = default_config()


def _cfg(**overrides):
    data = CFG.model_dump()
    data["weights"] = CFG.weights.model_dump()
    data["invalidation"] = CFG.invalidation.model_dump()
    data.update(overrides)
    return config_from_mapping(data)


def _decision(scenario: str, at: datetime = AT):
    features = SCENARIO_BUILDERS[scenario](at)
    return evaluate(features, CFG, SignalMemory().snapshot(), at), features


def _engine(config=None) -> PaperTradingEngine:
    return PaperTradingEngine(config or CFG, data_origin="LIVE")


def _open_ce(engine=None):
    engine = engine or _engine()
    decision, features = _decision("strong_ce")
    position = engine.open_from_decision(decision, features, "sig-ce-1", now=AT)
    return engine, position, decision, features


# --- entry ----------------------------------------------------------------
def test_ce_setup_opens_a_paper_ce_position_from_backend_prices():
    engine, position, _, features = _open_ce()
    assert position.option_type == "CE"
    assert position.state == PAPER_OPEN
    assert position.strike == features.atm.atm_strike
    assert position.entry_price == round(features.atm.atm_ce.ltp, 2)
    assert position.target_price == round(position.entry_price + CFG.target_points, 2)
    assert position.stoploss_price == round(position.entry_price - CFG.stoploss_points, 2)
    assert position.data_origin_label == "LIVE • SIMULATED DATA"
    assert "PAPER" in position.label.upper()
    assert engine.open_positions() == [position]


def test_pe_setup_opens_a_paper_pe_position():
    engine = _engine()
    decision, features = _decision("strong_pe")
    position = engine.open_from_decision(decision, features, "sig-pe-1", now=AT)
    assert position.option_type == "PE"
    assert position.entry_price == round(features.atm.atm_pe.ltp, 2)


def test_wait_decision_never_opens_a_position():
    engine = _engine()
    decision, features = _decision("weak")
    assert engine.open_from_decision(decision, features, "sig-wait", now=AT) is None
    assert engine.positions == []


def test_target_and_stop_reuse_the_phase_c_configuration():
    config = _cfg(target_points=50, stoploss_points=40)
    engine = _engine(config)
    decision, features = _decision("strong_ce")
    position = engine.open_from_decision(decision, features, "sig-cfg", now=AT)
    assert position.target_price == round(position.entry_price + 50, 2)
    assert position.stoploss_price == round(position.entry_price - 40, 2)


# --- exits ----------------------------------------------------------------
def test_target_exit_and_pnl():
    engine, position, _, _ = _open_ce()
    price = position.target_price + 1
    closed = engine.update(OptionObservation(timestamp=AT + timedelta(minutes=5), ltp=price), AT + timedelta(minutes=5))
    assert closed.state == PAPER_TARGET
    assert closed.exit_price == round(price, 2)
    assert closed.gross_points == round(price - position.entry_price, 2)
    assert closed.gross_pnl == round(closed.gross_points * closed.quantity, 2)
    assert closed.net_pnl == round(closed.gross_points * closed.quantity - closed.costs, 2)
    assert engine.open_positions() == []
    assert engine.trades() == [closed]


def test_stoploss_exit_and_negative_pnl():
    engine, position, _, _ = _open_ce()
    price = position.stoploss_price - 1
    closed = engine.update(OptionObservation(timestamp=AT, ltp=price), AT)
    assert closed.state == PAPER_STOPLOSS
    assert closed.gross_points < 0 and closed.net_pnl < 0


def test_same_observation_target_and_stop_is_ambiguous():
    engine, position, _, _ = _open_ce()
    obs = OptionObservation(
        timestamp=AT, ltp=position.entry_price,
        high=position.target_price + 2, low=position.stoploss_price - 2,
    )
    closed = engine.update(obs, AT)
    assert closed.state == PAPER_AMBIGUOUS
    assert "same observation" in closed.exit_reason


def test_invalidation_uses_the_backend_decision():
    engine, position, _, _ = _open_ce()
    decision, features = _decision("ce_below_vwap")
    # an explicit backend invalidation closes the paper position
    closed = engine.update(
        OptionObservation(timestamp=AT, ltp=position.entry_price - 2), AT, invalidated=True
    )
    assert closed.state == PAPER_INVALIDATED
    assert "invalidation" in closed.exit_reason.lower()
    assert closed.net_pnl is not None


def test_expiry_closes_the_position():
    engine, position, _, _ = _open_ce()
    closed = engine.update(OptionObservation(timestamp=AT, ltp=position.entry_price + 3), AT, expired=True)
    assert closed.state == PAPER_EXPIRED


def test_missing_entry_price_yields_no_data_and_no_invented_entry():
    engine = _engine()
    decision, features = _decision("strong_ce")
    stripped = features.model_copy(update={"atm": features.atm.model_copy(update={"atm_ce": None})})
    position = engine.open_from_decision(decision, stripped, "sig-nodata", now=AT)
    assert position.state == PAPER_NO_DATA
    assert position.entry_price is None and position.target_price is None
    assert engine.open_positions() == []


def test_missing_observation_does_not_fabricate_an_exit():
    engine, position, _, _ = _open_ce()
    still_open = engine.update(None, AT + timedelta(minutes=5))
    assert still_open.state == PAPER_OPEN
    assert still_open.exit_price is None


def test_invalidation_without_price_records_no_exit_price():
    engine, position, _, _ = _open_ce()
    closed = engine.update(None, AT, invalidated=True)
    assert closed.state == PAPER_INVALIDATED
    assert closed.exit_price is None and closed.net_pnl is None


# --- duplicate / limits ---------------------------------------------------
def test_same_signal_creates_only_one_position():
    engine, position, decision, features = _open_ce()
    assert engine.open_from_decision(decision, features, "sig-ce-1", now=AT) is None
    assert len(engine.positions) == 1


def test_max_open_positions_is_respected():
    engine, position, decision, features = _open_ce()
    assert engine.config.max_open_paper_positions == 1
    assert engine.open_from_decision(decision, features, "sig-ce-2", now=AT) is None
    assert len(engine.open_positions()) == 1


def test_repeated_polling_does_not_duplicate_positions():
    engine = _engine()
    decision, features = _decision("strong_ce")
    for _ in range(5):
        engine.on_tick(decision, features, None, "sig-poll", now=AT)
    assert len(engine.positions) == 1


# --- observation helper + summary ----------------------------------------
def test_observation_for_missing_strike_returns_none():
    assert observation_for(None, 25000, "CE", AT) is None


def test_summary_counts_and_labels_are_hypothetical():
    engine, position, _, _ = _open_ce()
    engine.update(OptionObservation(timestamp=AT, ltp=position.target_price + 1), AT)
    summary = engine.summary()
    assert summary.total_positions == 1 and summary.target == 1
    assert summary.open_positions == 0
    assert summary.net_pnl == engine.trades()[0].net_pnl
    assert "HYPOTHETICAL" in summary.cost_note.upper()
    assert "PAPER" in summary.label.upper()
    assert summary.data_origin_label == "LIVE • SIMULATED DATA"


def test_paper_engine_has_no_broker_surface():
    source = open("/app/backend/paper/paper_engine.py").read().lower()
    for banned in ("place_order", "buy_order", "sell_order", "modify_order",
                   "cancel_order", "dhanhq", "order_id"):
        assert banned not in source, banned
