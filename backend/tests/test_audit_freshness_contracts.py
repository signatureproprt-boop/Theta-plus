"""Audit regressions: synthetic data and mock broker only."""
from datetime import timedelta
from engines.fixtures import default_config, strong_ce_features
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from execution.instruments import InstrumentMap
from pipeline import Pipeline
from replay.fixtures import synthetic_session
from replay.outcome_engine import observations_from_snapshots
from test_phase_h_execution import AT, attempt, live_cfg, service


def seeded():
    p = Pipeline(default_config())
    f = strong_ce_features()
    p.features = f
    p.decision = evaluate_and_apply(SignalMemory(), f, p.config, f.timestamp)
    assert p.decision.decision == 'CE_SETUP'
    return p, f.timestamp


async def test_outage_blocks_previously_actionable_setup():
    p, at = seeded()
    async def fail(now):
        raise RuntimeError('fixture feed disconnected')
    p.feed.get_snapshot = fail
    await p.tick(at + timedelta(seconds=1))
    assert p.decision.decision == 'WAIT'
    assert p.decision.qualifying_side is None
    assert p.features.data_health.status.value == 'STALE'
    assert any('DATA_BLOCKED' in r for r in p.decision.reasons)


def test_read_time_expiry_blocks_setup_without_another_tick():
    p, at = seeded()
    p.refresh_data_health(at + timedelta(seconds=p.config.max_data_age_seconds + 1))
    assert p.decision.decision == 'WAIT'
    assert p.features.data_health.status.value == 'STALE'


async def test_successful_tick_recovers_health_after_outage():
    p, at = seeded()
    p._feed_failed = True
    p.refresh_data_health(at)
    await p.tick(at + timedelta(seconds=5))
    assert not p._feed_failed
    assert p.features.data_health.checks['DATA_AGE'] == 'OK'


def test_same_strike_requires_exact_expiry():
    rows = [dict(underlying='NIFTY', strike=25050, option_type='CALL',
                 security_id=sid, expiry=exp)
            for sid, exp in [('old', '2025-01-23'), ('new', '2025-01-30')]]
    mapping = InstrumentMap(rows)
    assert mapping.resolve('NIFTY', 25050, 'CE') is None
    assert mapping.resolve('NIFTY', 25050, 'CE', '2025-01-30').security_id == 'new'
    assert mapping.resolve('NIFTY', 25050, 'CE', '2025-02-06') is None


async def test_expired_contract_cannot_pass_live_gate(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    verdict = await attempt(svc, at=AT.replace(year=2026), signal_id='expired-fixture')
    assert not verdict.approved
    assert not svc.orders


async def test_missing_feature_expiry_fails_closed(monkeypatch):
    svc = service(monkeypatch, live_cfg())
    f = strong_ce_features().model_copy(update={'expiry': ''})
    d = evaluate_and_apply(SignalMemory(), f, svc.config, f.timestamp)
    verdict = await svc.attempt_entry(d, f, 'missing-expiry', now=f.timestamp)
    assert not verdict.approved
    assert not svc.orders


def test_replay_observations_isolate_contract_and_session():
    s = synthetic_session()[0]
    expiry = '2025-01-30'
    s = s.model_copy(update={'chain': s.chain.model_copy(update={'expiry': expiry})})
    def candidate(delta, exp=expiry, instrument='NIFTY'):
        return s.model_copy(update={'timestamp': s.timestamp + delta,
            'instrument': instrument, 'chain': s.chain.model_copy(update={'expiry': exp})})
    valid = candidate(timedelta(seconds=5))
    wrong_expiry = candidate(timedelta(seconds=10), '2025-02-06')
    next_session = candidate(timedelta(days=1))
    wrong_index = candidate(timedelta(seconds=15), instrument='BANKNIFTY')
    obs = observations_from_snapshots([valid, wrong_expiry, next_session, wrong_index],
        s.chain.rows[0].strike, 'CE', s.timestamp, expiry=expiry)
    assert len(obs) == 1
    assert obs[0].timestamp == valid.timestamp
