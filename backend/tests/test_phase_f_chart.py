"""Phase F — chart visualization tests.

Covers: marker mapping from backend decisions, CE/PE/invalidation markers,
WAIT hidden by default, timestamp/IST handling and ordering, LIVE vs
REPLAY-SYNTHETIC labels, backend VWAP authority, stale handling, the
/api/chart/signals API surface, and the absence of any execution path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from chart.markers import (
    link_invalidations,
    marker_from_decision,
    marker_from_replay_signal,
    origin_label,
    visible_markers,
)
from engines.fixtures import SCENARIO_BUILDERS
from engines.signal_engine import evaluate, evaluate_and_apply
from engines.signal_memory import SignalMemory
from lib.config import get_config
from lib.dates import IST
from models.chart_models import (
    DEFAULT_VISIBLE_DECISIONS,
    ORIGIN_LIVE,
    ORIGIN_REPLAY_SYNTHETIC,
)
from replay.backtest_engine import run_replay
from replay.fixtures import synthetic_session
from server import app
from storage.sources import InMemorySource

AT = datetime(2025, 1, 15, 12, 0, tzinfo=IST)


def _decision(scenario: str, at: datetime = AT):
    config = get_config()
    features = SCENARIO_BUILDERS[scenario](at)
    return evaluate(features, config, SignalMemory().snapshot(), at), features


# --- marker mapping -------------------------------------------------------
def test_ce_setup_produces_a_setup_marker_with_backend_values():
    decision, features = _decision("strong_ce")
    assert decision.decision == "CE_SETUP"
    marker = marker_from_decision(decision, features)
    assert marker.decision == "CE_SETUP"
    assert marker.marker_kind == "SETUP"
    assert marker.side == "CE"
    assert marker.score == decision.ce_score
    assert marker.max_score == 100
    # backend values, not recomputed anywhere else
    assert marker.spot == decision.spot
    assert marker.vwap == features.vwap.vwap
    assert marker.vwap_distance == features.vwap.distance
    assert marker.pcr == decision.pcr
    assert marker.atm_pcr == features.pcr.atm_pcr
    assert marker.ce_oi == features.oi.call_oi
    assert marker.pe_oi_change == features.oi.put_oi_change
    assert marker.strategy_version == decision.strategy_version
    assert marker.feature_version == decision.feature_engine_version


def test_pe_setup_produces_pe_marker():
    decision, features = _decision("strong_pe")
    assert decision.decision == "PE_SETUP"
    marker = marker_from_decision(decision, features)
    assert marker.side == "PE"
    assert marker.marker_kind == "SETUP"
    assert marker.score == decision.pe_score


def test_wait_marker_kind_is_wait_and_not_default_visible():
    decision, features = _decision("weak")
    marker = marker_from_decision(decision, features)
    assert marker.decision == "WAIT"
    assert marker.marker_kind == "WAIT"
    assert marker.decision not in DEFAULT_VISIBLE_DECISIONS
    assert visible_markers([marker]) == []
    assert visible_markers([marker], include_wait=True) == [marker]


def test_invalidation_marker_keeps_original_signal_identity():
    """A CE setup then a VWAP-cross invalidation: both markers survive."""
    config = get_config()
    memory = SignalMemory()
    t0 = AT
    setup = evaluate_and_apply(memory, SCENARIO_BUILDERS["strong_ce"](t0), config, t0)
    assert setup.decision == "CE_SETUP"
    # ramp to ACTIVE exactly like the live pipeline does before invalidation applies
    for minutes in (10, 20):
        evaluate_and_apply(memory, SCENARIO_BUILDERS["strong_ce"](t0 + timedelta(minutes=minutes)),
                           config, t0 + timedelta(minutes=minutes))
    t1 = t0 + timedelta(minutes=21)
    inval = evaluate_and_apply(memory, SCENARIO_BUILDERS["ce_below_vwap"](t1), config, t1)
    assert inval.decision == "CE_INVALIDATED"

    markers = link_invalidations([
        marker_from_decision(setup, None),
        marker_from_decision(inval, None),
    ])
    assert [m.marker_kind for m in markers] == ["SETUP", "INVALIDATION"]
    # the historical setup marker is preserved, not deleted or mutated
    assert markers[0].decision == "CE_SETUP"
    assert markers[1].origin_signal_id == markers[0].signal_id
    assert markers[1].invalidation_timestamp == markers[1].timestamp
    assert markers[1].invalidation_reason


def test_stale_features_mark_the_marker_stale():
    decision, features = _decision("stale")
    marker = marker_from_decision(decision, features)
    assert marker.decision == "WAIT"
    assert marker.stale is True
    assert "STALE" in marker.data_health.upper()


# --- timestamps -----------------------------------------------------------
def test_utc_timestamp_is_converted_to_ist():
    utc_at = datetime(2025, 1, 15, 6, 30, tzinfo=timezone.utc)  # 12:00 IST
    decision, features = _decision("strong_ce", utc_at)
    marker = marker_from_decision(decision, features)
    assert marker.timestamp.utcoffset() == timedelta(hours=5, minutes=30)
    assert (marker.timestamp.hour, marker.timestamp.minute) == (12, 0)


def test_naive_timestamp_is_treated_as_ist():
    naive = datetime(2025, 1, 15, 12, 0)
    decision, features = _decision("strong_ce", naive)
    marker = marker_from_decision(decision, features)
    assert marker.timestamp.tzinfo is not None
    assert (marker.timestamp.hour, marker.timestamp.minute) == (12, 0)


def test_markers_are_returned_in_chronological_order():
    config = get_config()
    built = []
    for minutes in (20, 0, 10):
        at = AT + timedelta(minutes=minutes)
        dec = evaluate(SCENARIO_BUILDERS["strong_ce"](at), config, SignalMemory().snapshot(), at)
        built.append(marker_from_decision(dec, None))
    ordered = visible_markers(built)
    assert [m.timestamp for m in ordered] == sorted(m.timestamp for m in built)


# --- origin labels --------------------------------------------------------
def test_origin_labels_never_claim_real_market_data():
    assert origin_label(ORIGIN_LIVE) == "LIVE • SIMULATED DATA"
    assert origin_label(ORIGIN_REPLAY_SYNTHETIC) == "REPLAY • SYNTHETIC DATA"
    assert "LIVE MARKET" not in origin_label(ORIGIN_LIVE).upper()


# --- replay-synthetic parity ---------------------------------------------
def test_replay_synthetic_markers_come_from_the_phase_e_engine():
    result = run_replay(InMemorySource(synthetic_session()), get_config(), data_origin="SYNTHETIC")
    markers = link_invalidations([marker_from_replay_signal(s) for s in result.signals])
    assert markers, "synthetic replay produced no signals"
    assert all(m.data_origin == ORIGIN_REPLAY_SYNTHETIC for m in markers)
    assert all(m.data_origin_label == "REPLAY • SYNTHETIC DATA" for m in markers)
    setups = [m for m in visible_markers(markers) if m.marker_kind == "SETUP"]
    assert setups, "expected at least one CE/PE setup marker in the synthetic session"
    # marker fields mirror the replay signal exactly (no recomputation)
    by_id = {s.signal_id: s for s in result.signals}
    for marker in setups:
        src = by_id[marker.signal_id]
        assert marker.decision == src.decision
        assert marker.state == src.state
        assert marker.vwap == src.vwap
        assert marker.pcr == src.total_pcr


# --- API ------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_chart_signals_live_endpoint(client):
    res = client.get("/api/chart/signals")
    assert res.status_code == 200
    body = res.json()
    assert body["symbol"] == "NIFTY"
    assert body["tradingview_symbol"] == "NSE:NIFTY"
    assert body["timeframe"] == "5"
    assert body["timezone"] == "Asia/Kolkata"
    assert body["data_origin"] == "LIVE"
    assert body["data_origin_label"] == "LIVE • SIMULATED DATA"
    assert body["include_wait"] is False
    assert all(m["decision"] in DEFAULT_VISIBLE_DECISIONS for m in body["markers"])
    assert any("SIMULATED" in n.upper() for n in body["notes"])


def test_chart_signals_hides_wait_by_default_and_can_include_it(client):
    default = client.get("/api/chart/signals").json()
    assert all(m["decision"] != "WAIT" for m in default["markers"])
    with_wait = client.get("/api/chart/signals?include_wait=true").json()
    assert with_wait["include_wait"] is True
    assert with_wait["count"] >= default["count"]


def test_chart_signals_replay_synthetic_endpoint(client):
    res = client.get("/api/chart/signals?origin=REPLAY-SYNTHETIC")
    assert res.status_code == 200
    body = res.json()
    assert body["data_origin"] == "REPLAY-SYNTHETIC"
    assert body["data_origin_label"] == "REPLAY • SYNTHETIC DATA"
    assert body["count"] >= 1
    assert any("SYNTHETIC" in n.upper() for n in body["notes"])
    marker = body["markers"][0]
    for key in ("signal_id", "timestamp", "decision", "state", "score", "vwap", "pcr",
                "strategy_version", "feature_version", "data_origin"):
        assert key in marker
    # backend VWAP is present and authoritative in the payload
    assert body["markers"][-1]["vwap"] is not None
    # IST offset preserved on the wire
    assert "+05:30" in marker["timestamp"]


def test_chart_signal_detail_and_404(client):
    body = client.get("/api/chart/signals?origin=REPLAY-SYNTHETIC").json()
    sid = body["markers"][0]["signal_id"]
    ok = client.get(f"/api/chart/signals/{sid}")
    assert ok.status_code == 200
    assert ok.json()["signal_id"] == sid
    missing = client.get("/api/chart/signals/does-not-exist")
    assert missing.status_code == 404


def test_invalid_origin_is_rejected(client):
    assert client.get("/api/chart/signals?origin=TRADINGVIEW").status_code == 422


def test_chart_api_contains_no_execution_surface(client):
    schema = client.get("/openapi.json").json()
    banned = ("order", "buy", "sell", "execute", "trade/place", "webhook")
    for path in schema["paths"]:
        if path.startswith("/api/chart"):
            assert not any(word in path.lower() for word in banned), path
    text = (
        open("/app/backend/routers/chart_router.py").read()
        + open("/app/backend/chart/markers.py").read()
        + open("/app/backend/models/chart_models.py").read()
    ).lower()
    for word in ("place_order", "buy_order", "sell_order", "modify_order", "cancel_order"):
        assert word not in text


def test_chart_modules_do_not_import_strategy_mutation():
    """Phase F only serializes: no rule/score/state logic is imported."""
    source = open("/app/backend/chart/markers.py").read()
    for banned in ("rule_engine", "score_engine", "state_machine", "pcr_engine", "vwap_engine"):
        assert banned not in source
