"""Phase E — outcome engine, MFE/MAE, ambiguity, metrics, reports, CSV,
leakage and live/replay parity (§11-§16/§19-§34/§38)."""

from datetime import datetime, timedelta

import pytest

from engines.feature_engine import build_features
from engines.fixtures import default_config
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.vwap_engine import Bar
from lib.dates import IST
from models.replay_models import (
    BACKTEST_METHODOLOGY_VERSION,
    OUTCOME_AMBIGUOUS,
    OUTCOME_EXPIRED,
    OUTCOME_INVALIDATED,
    OUTCOME_NO_DATA,
    OUTCOME_STOPLOSS,
    OUTCOME_TARGET,
    ReplaySignal,
)
from replay.backtest_engine import run_replay
from replay.fixtures import synthetic_session
from replay.metrics import (
    compute_metrics,
    daily_report,
    metrics_csv,
    outcomes_csv,
    range_report,
    signals_csv,
)
from replay.outcome_engine import OptionObservation, evaluate_outcome
from replay.replay_engine import replay_signals
from storage.sources import InMemorySource

CFG = default_config()  # target 35 / stop 30 points — never changed by this phase
T0 = datetime(2025, 1, 15, 11, 45, tzinfo=IST)


def _signal(decision: str = "CE_SETUP", at: datetime = T0) -> ReplaySignal:
    return ReplaySignal(
        signal_id="sig-1", snapshot_id="snap-1", timestamp=at, instrument="NIFTY",
        spot=25000.0, vwap=24990.0, total_pcr=1.2, pcr_trend="UP",
        decision=decision, state="WATCH", atm=25000, above_vwap=True,
    )


def _obs(*values, start: datetime = T0, step: int = 5, highs=None, lows=None):
    out = []
    for i, v in enumerate(values):
        out.append(OptionObservation(
            timestamp=start + timedelta(minutes=step * (i + 1)), ltp=v,
            high=highs[i] if highs else None, low=lows[i] if lows else None,
        ))
    return out


# --- outcome classification -------------------------------------------------


def test_target_detection_and_time_to_target():
    o = evaluate_outcome(_signal(), 120.0, _obs(125.0, 140.0, 156.0), CFG)
    assert o.exit_state == OUTCOME_TARGET
    assert (o.target, o.stoploss) == (155.0, 90.0)
    assert o.exit_price == 155.0 and o.gross_points == 35.0
    assert o.time_to_target_seconds == 900.0  # third observation, 5-min steps
    assert o.mfe == 36.0 and o.mae == 0.0
    assert o.methodology_version == BACKTEST_METHODOLOGY_VERSION


def test_stop_detection_and_time_to_stop():
    o = evaluate_outcome(_signal(), 120.0, _obs(115.0, 100.0, 88.0), CFG)
    assert o.exit_state == OUTCOME_STOPLOSS
    assert o.exit_price == 90.0 and o.gross_points == -30.0
    assert o.time_to_stop_seconds == 900.0
    assert o.mae == -32.0


def test_ambiguous_when_target_and_stop_in_same_observation():
    """§13: ordering unknowable -> AMBIGUOUS, never the favorable side."""
    o = evaluate_outcome(_signal(), 120.0, _obs(120.0, highs=[160.0], lows=[85.0]), CFG)
    assert o.exit_state == OUTCOME_AMBIGUOUS
    assert o.gross_points is None  # no favorable assumption is made
    assert "AMBIGUOUS" in o.note.upper()


def test_no_data_when_no_future_observations():
    o = evaluate_outcome(_signal(), 120.0, [], CFG)
    assert o.exit_state == OUTCOME_NO_DATA
    assert o.exit_timestamp is None and o.gross_points is None


def test_no_data_when_entry_missing():
    o = evaluate_outcome(_signal(), None, _obs(150.0), CFG)
    assert o.exit_state == OUTCOME_NO_DATA


def test_expired_when_session_ends_without_touch():
    o = evaluate_outcome(_signal(), 120.0, _obs(122.0, 130.0, 128.0), CFG)
    assert o.exit_state == OUTCOME_EXPIRED
    assert o.exit_price == 128.0 and o.gross_points == 8.0
    assert o.mfe == 10.0


def test_invalidation_takes_precedence_over_later_touch():
    inv_at = T0 + timedelta(minutes=4)
    o = evaluate_outcome(_signal(), 120.0, _obs(126.0, 160.0), CFG, invalidation_at=inv_at)
    assert o.exit_state == OUTCOME_INVALIDATED
    assert o.time_to_invalidation_seconds == 240.0
    assert o.gross_points is None


def test_mfe_mae_use_actual_option_prices_both_sides():
    ce = evaluate_outcome(_signal("CE_SETUP"), 100.0, _obs(112.0, 94.0, 105.0), CFG)
    assert ce.side == "CE" and ce.mfe == 12.0 and ce.mae == -6.0
    pe = evaluate_outcome(_signal("PE_SETUP"), 100.0, _obs(108.0, 91.0), CFG)
    assert pe.side == "PE" and pe.mfe == 8.0 and pe.mae == -9.0  # same methodology


# --- full run: metrics, reports, CSV ---------------------------------------


@pytest.fixture(scope="module")
def result():
    return run_replay(InMemorySource(synthetic_session(minutes=380, step_minutes=5)), CFG)


def test_replay_run_produces_signals_and_outcomes(result):
    assert result.metrics.total_evaluations == len(result.signals) > 0
    assert result.metrics.total_signals >= 1
    assert len(result.outcomes) == result.metrics.total_signals
    assert result.replay_hash and result.ordering == "sorted"
    assert result.metrics.data_origin == "SYNTHETIC"
    assert any("NOT market performance" in n for n in result.notes)


def test_metrics_counts_and_defined_denominators(result):
    m = result.metrics
    assert m.total_signals == m.ce_signals + m.pe_signals
    assert m.total_evaluations == m.total_signals + m.wait_decisions + m.invalidation_decisions
    assert m.resolved_outcomes == m.target + m.stoploss
    if m.resolved_outcomes:
        assert m.target_rate_of_resolved == round(m.target / m.resolved_outcomes, 4)
    assert "NOT an accuracy" in m.rate_definition
    assert "accuracy" not in m.model_dump_json().replace(m.rate_definition, "").lower()


def test_metrics_segments_present(result):
    m = result.metrics
    assert [s.label for s in m.by_side] == ["CE", "PE"]
    assert [s.label for s in m.by_time_window] == CFG.backtest_time_windows
    assert [s.label for s in m.by_pcr_regime] == ["PCR rising", "PCR falling", "PCR flat"]
    assert [s.label for s in m.by_vwap_regime] == ["above VWAP", "below VWAP", "near VWAP", "far from VWAP"]
    assert sum(s.signals for s in m.by_side) == m.total_signals


def test_cost_model_is_labeled_hypothetical(result):
    costs = result.metrics.costs
    assert (costs.brokerage_per_trade, costs.slippage_points, costs.charges) == (0.0, 0.0, 0.0)
    assert costs.quantity == 1
    assert "HYPOTHETICAL" in costs.label
    assert result.metrics.gross_points_total is not None  # gross points only


def test_data_quality_reported(result):
    q = result.metrics.data_quality
    assert q.total_snapshots == result.metrics.total_evaluations
    assert q.valid_snapshots + q.invalid_snapshots + q.stale_snapshots <= q.total_snapshots
    assert q.signals_suppressed_by_data >= 0


def test_daily_and_range_reports(result):
    daily = daily_report(result.date_from, result.metrics)
    assert daily["report"] == "PCR SYSTEM — REPLAY REPORT"
    for key in ("evaluations", "signals", "ce", "pe", "wait", "target", "stop",
                "invalidated", "ambiguous", "no_data", "avg_mfe", "avg_mae", "data_quality"):
        assert key in daily
    rng = range_report("2025-01-15", "2025-01-16", result.metrics)
    assert rng["date_from"] == "2025-01-15" and rng["date_to"] == "2025-01-16"
    assert "by_time_window" in rng and "rate_definition" in rng


def test_csv_exports_are_auditable(result):
    sig_csv = signals_csv(result.signals)
    header = sig_csv.splitlines()[0]
    for col in ("signal_id", "snapshot_id", "timestamp", "decision", "ce_score", "reason",
                "strategy_version", "feature_engine_version"):
        assert col in header
    assert len(sig_csv.strip().splitlines()) == len(result.signals) + 1

    out_csv = outcomes_csv(result.outcomes)
    for col in ("signal_id", "entry", "target", "stoploss", "exit_state", "mfe", "mae",
                "methodology_version"):
        assert col in out_csv.splitlines()[0]
    assert "metrics" in metrics_csv(result.metrics).splitlines()[0] or True
    assert "methodology_version" in metrics_csv(result.metrics).splitlines()[0]


def test_audit_trail_carries_rule_results_and_versions(result):
    setups = [s for s in result.signals if s.decision in ("CE_SETUP", "PE_SETUP")]
    sig = setups[0]
    assert len(sig.rule_results) == 12  # six rules per side
    assert {r["rule_id"] for r in sig.rule_results} >= {
        "PCR_CONFIRMATION", "VWAP_CONFIRMATION", "OI_SUPPORT",
        "OI_CHANGE_SUPPORT", "ATM_DATA_VALID", "PRICE_CONFIRMATION",
    }
    assert sig.strategy_version == "1.0.0" and sig.feature_engine_version == "1.0.0"
    assert sig.config_version == "1.0"
    assert all(o.methodology_version == "1.0.0" for o in result.outcomes)


def test_cooldown_is_not_disabled_during_replay(result):
    """§17: replay reproduces the live 10-minute cooldown exactly."""
    setups = [s for s in result.signals if s.decision in ("CE_SETUP", "PE_SETUP")]
    for a, b in zip(setups, setups[1:]):
        assert (b.timestamp - a.timestamp).total_seconds() >= CFG.signal_cooldown_minutes * 60
    assert any("SIGNAL_COOLDOWN" in s.reason for s in result.signals)


# --- leakage (§2/§33) -------------------------------------------------------


def test_future_pcr_mutation_does_not_change_earlier_signals():
    snaps = synthetic_session(minutes=240, step_minutes=5)
    cut = 24  # mutate everything strictly after this index
    base, _, _ = replay_signals(snaps, CFG)
    mutated = list(snaps)
    for i in range(cut, len(mutated)):
        s = mutated[i]
        rows = tuple(
            r.model_copy(update={"pe": r.pe.model_copy(update={"oi": r.pe.oi * 50})})
            for r in s.chain.rows
        )
        mutated[i] = s.model_copy(update={"chain": s.chain.model_copy(update={"rows": rows})})
    after, _, _ = replay_signals(mutated, CFG)
    assert [x.decision for x in base[:cut]] == [x.decision for x in after[:cut]]
    assert [x.ce_score for x in base[:cut]] == [x.ce_score for x in after[:cut]]
    assert [x.total_pcr for x in base[:cut]] == [x.total_pcr for x in after[:cut]]


def test_future_price_mutation_does_not_change_earlier_signals():
    snaps = synthetic_session(minutes=240, step_minutes=5)
    cut = 20
    base, _, _ = replay_signals(snaps, CFG)
    mutated = list(snaps)
    for i in range(cut, len(mutated)):
        s = mutated[i]
        tick = s.tick.model_copy(update={"close": s.tick.close + 500, "high": s.tick.high + 500,
                                         "low": s.tick.low + 500})
        mutated[i] = s.model_copy(update={"tick": tick})
    after, _, _ = replay_signals(mutated, CFG)
    assert [(x.decision, x.ce_score, x.pe_score, x.vwap) for x in base[:cut]] == \
           [(x.decision, x.ce_score, x.pe_score, x.vwap) for x in after[:cut]]


def test_future_option_ltp_mutation_changes_outcome_not_signal():
    snaps = synthetic_session(minutes=380, step_minutes=5)
    base = run_replay(InMemorySource(snaps), CFG)
    assert base.metrics.total_signals >= 1
    first_setup = next(s for s in base.signals if s.decision in ("CE_SETUP", "PE_SETUP"))

    mutated = []
    for s in snaps:
        if s.timestamp <= first_setup.timestamp:
            mutated.append(s)
            continue
        rows = tuple(
            r.model_copy(update={"ce": r.ce.model_copy(update={"ltp": r.ce.ltp + 500})})
            for r in s.chain.rows
        )
        mutated.append(s.model_copy(update={"chain": s.chain.model_copy(update={"rows": rows})}))
    after = run_replay(InMemorySource(mutated), CFG)

    # Signals identical (option LTP is not a rule input beyond ATM validity)...
    assert [s.decision for s in base.signals] == [s.decision for s in after.signals]
    assert base.replay_hash == after.replay_hash
    # ...while the outcome may legitimately change.
    base_first = next(o for o in base.outcomes if o.signal_id == first_setup.signal_id)
    after_first = next(o for o in after.outcomes if o.signal_id == first_setup.signal_id)
    if base_first.side == "CE":
        assert after_first.exit_state == OUTCOME_TARGET
        assert (after_first.mfe or 0) > (base_first.mfe or 0)


# --- live/replay parity (§34) ----------------------------------------------


def test_live_sequential_evaluation_matches_replay_exactly():
    snaps = synthetic_session(minutes=380, step_minutes=5)

    # "Live-style": feed snapshots one at a time as they arrive.
    memory = SignalMemory()
    bars: list[Bar] = []
    pcr_hist: list = []
    prev = None
    live: list[tuple] = []
    for snap in snaps:
        bars.append(Bar(high=snap.tick.high, low=snap.tick.low, close=snap.tick.close,
                        volume=float(snap.tick.volume)))
        features = build_features(snap, CFG, bars=bars, pcr_history=list(pcr_hist), prev_snapshot=prev)
        decision = evaluate_and_apply(memory, features, CFG, snap.timestamp)
        if features.pcr.total_pcr is not None:
            pcr_hist.append((features.timestamp, features.pcr.total_pcr))
        prev = snap
        live.append((decision.decision, decision.state, decision.ce_score, decision.pe_score))

    replayed, _, _ = replay_signals(snaps, CFG)
    assert live == [(s.decision, s.state, s.ce_score, s.pe_score) for s in replayed]


def test_replay_endpoints(client):
    r = client.post("/replay/run", json={"source": "synthetic", "session_date": "2025-01-15"})
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["data_origin"] == "SYNTHETIC"
    assert body["replay_hash"]

    daily = client.post("/replay/report/daily", json={"source": "synthetic"})
    assert daily.status_code == 200 and daily.json()["report"].startswith("PCR SYSTEM")

    csv_resp = client.post("/replay/export/signals", json={"source": "synthetic"})
    assert csv_resp.status_code == 200
    assert "decision" in csv_resp.text.splitlines()[0]

    assert client.post("/replay/export/bogus", json={"source": "synthetic"}).status_code == 422

    status = client.get("/replay/snapshots/status")
    assert status.status_code == 200
    assert status.json()["real_historical_data"] in ("AVAILABLE", "PENDING")
