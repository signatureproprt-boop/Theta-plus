"""Exposure reservations and closed-session candle regressions."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import math
import pytest
from data.session_candles import closed_session_bars
from engines.vwap_engine import Bar, compute_vwap
from execution.adapter import BrokerOrderState
from models.execution_models import BLOCK_MAX_POSITIONS
from test_phase_h_execution import AT, attempt, live_cfg, service


@pytest.mark.parametrize('behaviour', ['filled', 'acknowledged', 'partial', 'timeout'])
async def test_filled_pending_and_uncertain_orders_reserve_position(monkeypatch, behaviour):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=10), behaviour=behaviour)
    first = await attempt(svc, signal_id='first')
    second = await attempt(svc, signal_id='second')
    assert first.approved
    assert not second.approved and second.reason == BLOCK_MAX_POSITIONS
    assert svc.adapter.submit_count == 1
    assert svc.risk_state.open_positions == 1


async def test_concurrent_signals_cannot_both_pass_last_slot(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=10))
    original = svc.adapter.submit_order
    async def delayed(order):
        await asyncio.sleep(.01)
        return await original(order)
    svc.adapter.submit_order = delayed
    results = await asyncio.gather(attempt(svc, signal_id='a'), attempt(svc, signal_id='b'))
    assert sum(r.approved for r in results) == 1
    assert svc.adapter.submit_count == 1


async def test_unfilled_rejection_releases_reservation(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=10), behaviour='rejected')
    await attempt(svc, signal_id='rejected')
    assert svc.risk_state.open_positions == 0
    svc.adapter.behaviour = 'filled'
    assert (await attempt(svc, signal_id='next')).approved


async def test_cancelled_remainder_does_not_release_partial_fill(monkeypatch):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=10))
    await attempt(svc, signal_id='filled')
    record = next(iter(svc.orders.values()))
    svc._apply_broker_state(record, BrokerOrderState(record.broker_order_id, 'CANCELLED',
        quantity=1, filled_quantity=1))
    assert svc.risk_state.open_positions == 1
    assert not (await attempt(svc, signal_id='another')).approved


def payload(at, volumes=(1, 3, 9)):
    return dict(timestamp=[(at + timedelta(minutes=i)).timestamp() for i in range(3)],
        open=[100, 200, 300], high=[100, 200, 300], low=[100, 200, 300],
        close=[100, 200, 300], volume=list(volumes))


def test_completed_candles_only_and_real_volume_weighting():
    start = AT.replace(hour=9, minute=15, second=0)
    bars = closed_session_bars(payload(start), start + timedelta(minutes=2, seconds=10), 'NIFTY_FUTURES')
    assert len(bars) == 2
    assert compute_vwap(bars) == 175
    assert bars[-1].source == 'NIFTY_FUTURES'


@pytest.mark.parametrize('kind', ['gap', 'duplicate', 'negative', 'nan', 'mismatch'])
def test_corrupt_candle_series_is_rejected(kind):
    start = AT.replace(hour=9, minute=15, second=0)
    data = payload(start)
    if kind == 'gap': data['timestamp'][1] += 60
    if kind == 'duplicate': data['timestamp'][1] = data['timestamp'][0]
    if kind == 'negative': data['volume'][0] = -1
    if kind == 'nan': data['close'][0] = math.nan
    if kind == 'mismatch': data['volume'].pop()
    with pytest.raises(ValueError):
        closed_session_bars(data, start + timedelta(minutes=3), 'NIFTY_FUTURES')


def test_no_mean_fallback_for_zero_or_invalid_volume():
    assert compute_vwap([Bar(100,100,100,0)]) is None
    assert compute_vwap([Bar(100,100,100,-1)]) is None
    assert compute_vwap([Bar(100,100,100,math.nan)]) is None


async def test_dhan_feed_uses_mapped_futures_candles_and_caches_minute(monkeypatch, tmp_path):
    import json
    from pipeline import DhanFeed
    from engines.fixtures import default_config
    start = AT.replace(hour=9, minute=15, second=0)
    now = start + timedelta(minutes=2, seconds=10)
    mapping = tmp_path / 'instruments.json'
    mapping.write_text(json.dumps({'instruments': [dict(underlying='NIFTY',
        instrument_type='FUTIDX', exchange_segment='NSE_FNO', security_id='verified-future',
        expiry='2025-01-30')]}))
    monkeypatch.setenv('DHAN_INSTRUMENT_MAP_PATH', str(mapping))
    feed = DhanFeed(default_config())
    calls = []
    async def candles(sid, segment, kind, at):
        calls.append((sid,segment,kind))
        return payload(start)
    feed.client.get_session_candles = candles
    await feed.refresh_candles(now)
    await feed.refresh_candles(now + timedelta(seconds=5))
    assert calls == [('13','IDX_I','INDEX'),('verified-future','NSE_FNO','FUTIDX')]
    assert len(feed.get_bars(now.date().isoformat())) == 2
    assert compute_vwap(feed.get_vwap_bars(now.date().isoformat())) == 175


async def test_missing_futures_mapping_never_uses_spot_volume(monkeypatch):
    from pipeline import DhanFeed
    from engines.fixtures import default_config
    monkeypatch.delenv('DHAN_INSTRUMENT_MAP_PATH', raising=False)
    start = AT.replace(hour=9, minute=15, second=0)
    feed = DhanFeed(default_config())
    async def candles(*args): return payload(start)
    feed.client.get_session_candles = candles
    await feed.refresh_candles(start + timedelta(minutes=2))
    assert feed.get_vwap_bars(start.date().isoformat()) == []


async def test_intraday_client_request_contract(monkeypatch):
    from data.dhan_client import DhanMarketData
    client = DhanMarketData(access_token='fixture', client_id='fixture')
    calls = []
    async def post(path, body):
        calls.append((path,body))
        return {}
    client._post = post
    await client.get_session_candles('mapped', 'NSE_FNO', 'FUTIDX', AT)
    path,body = calls[0]
    assert path == '/v2/charts/intraday'
    assert body['securityId'] == 'mapped'
    assert body['instrument'] == 'FUTIDX' and body['interval'] == '1'
    assert body['fromDate'].endswith('09:15:00')
