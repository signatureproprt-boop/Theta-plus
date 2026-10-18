from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
import server
from engine import replay, VERSION, IST
from models import Candle, Contract, Dataset, Experiment, Snapshot

AT=datetime(2025,1,15,10,0,tzinfo=IST)

class Clock(datetime):
    @classmethod
    def now(cls,tz=None):
        return AT.astimezone(tz or timezone.utc)

def snapshot(at=AT,source='fixture',bid=100):
    return Snapshot(timestamp=at,spot=25000,source=source,contracts=[Contract(
        security_id='ce-one',expiry=at.date()+timedelta(days=7),strike=25000,side='CE',
        lot_size=1,bid=bid,ask=bid+1,bid_qty=100,ask_qty=100,oi=100,volume=100,quote_at=at)])

def candles():
    start=AT.replace(hour=9,minute=15)
    return [Candle(timestamp=start+timedelta(minutes=i),open=25000+i,
        high=25002+i,low=24999+i,close=25001+i,volume=10) for i in range(60)]

@pytest.fixture
def app(tmp_path,monkeypatch):
    for name in ('THETA_AUTH_MODE','K_SERVICE','THETA_GCS_BUCKET','DHAN_ACCESS_TOKEN','THETA_DHAN_TOKEN_SECRET','DHAN_CLIENT_ID'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('THETA_API_KEY','fixture-key')
    monkeypatch.setattr(server,'datetime',Clock)
    return server.create_app(tmp_path/'test.sqlite3')

def client(app):
    return TestClient(app,headers={'Authorization':'Bearer fixture-key'})

def seed(app,snap):
    app.state.store.add('live_snapshot',snap.model_dump(mode='json'))
    app.state.store.add('live_candles',{'candles':[c.model_dump(mode='json') for c in candles()]})

def plan():
    return dict(security_id='ce-one',capital=10000,risk_budget=100,stop_quote=90,
                costs_per_lot=0,slippage_per_unit=0,max_spread_pct=2)

def test_health_and_auth_boundary_preserved(app):
    c=TestClient(app)
    assert c.get('/health-check').json()==dict(status='ok',version=VERSION,execution='NO_BROKER_EXECUTION',revision='local')
    assert c.get('/api/status').status_code==401
    assert 'page-room' in c.get('/').text
    assert client(app).get('/api/status').json()['execution']=='NO_BROKER_EXECUTION'

@pytest.mark.parametrize('seconds',[-61,1,120])
def test_stale_or_future_provider_observation_blocks_plan(app,seconds):
    seed(app,snapshot(AT+timedelta(seconds=seconds)))
    c=client(app)
    assert c.get('/api/overview').json()['decision']=='WAIT'
    assert c.post('/api/plans',json=plan()).status_code==409

def test_fresh_plan_remains_available(app):
    seed(app,snapshot())
    assert client(app).post('/api/plans',json=plan()).json()['allowed']

def test_expired_option_cannot_be_planned(app):
    snap=snapshot()
    snap.contracts[0].expiry=AT.date()-timedelta(days=1)
    seed(app,snap)
    assert client(app).post('/api/plans',json=plan()).json()['allowed'] is False

def test_provider_failure_blocks_recent_snapshot(app,monkeypatch):
    seed(app,snapshot())
    monkeypatch.setenv('DHAN_ACCESS_TOKEN','fixture-token')
    monkeypatch.setenv('DHAN_CLIENT_ID','fixture-client')
    async def catalog(self):return None
    async def fail(self,metadata):raise server.ProviderError('fixture outage')
    monkeypatch.setattr(server.MarketBoard,'catalog',catalog)
    monkeypatch.setattr(server.Dhan,'refresh',fail)
    c=client(app)
    assert c.post('/api/refresh',json={}).status_code==503
    assert c.get('/api/overview').json()['decision']=='WAIT'
    assert c.post('/api/plans',json=plan()).status_code==409

def experiment():
    return Experiment(dataset_id=1,route='chart',strategy='structure',risk_per_trade=100,
        capital=10000,stop_pct=10,reward_multiple=2,costs_per_lot=0,slippage_per_unit=0,max_hold_minutes=30,max_spread_pct=2)

def test_unknown_exit_blocks_new_entries():
    snaps=[snapshot(AT+timedelta(seconds=s)) for s in (0,30,200,230,260,290)]
    result=replay(Dataset(name='fixture',provenance='synthetic regression only',candles=candles(),snapshots=snaps),experiment())
    assert result['summary']['entries']==1
    assert result['summary']['unresolved']==1
    assert result['trades'][0]['reason']=='UNRESOLVED_DATA_GAP'

def test_source_switch_cannot_supply_exit_price():
    snaps=[snapshot(),snapshot(AT+timedelta(seconds=30)),snapshot(AT+timedelta(seconds=60),source='other',bid=200)]
    result=replay(Dataset(name='fixture',provenance='synthetic regression only',candles=candles(),snapshots=snaps),experiment())
    assert result['summary']['entries']==1
    assert result['summary']['unresolved']==1
    assert result['trades'][0]['net_pnl'] is None

def test_replay_hash_includes_contract_expiry():
    data=Dataset(name='fixture',provenance='synthetic regression only',candles=candles(),snapshots=[snapshot()])
    first=replay(data,experiment())['dataset_sha256']
    data.snapshots[0].contracts[0].expiry+=timedelta(days=1)
    assert replay(data,experiment())['dataset_sha256']!=first


def test_expiry_switch_cannot_supply_exit_price():
    snaps=[snapshot(),snapshot(AT+timedelta(seconds=30)),snapshot(AT+timedelta(seconds=60),bid=200)]
    snaps[-1].contracts[0].expiry+=timedelta(days=7)
    result=replay(Dataset(name='fixture',provenance='synthetic regression only',candles=candles(),snapshots=snaps),experiment())
    assert result['summary']['unresolved']==1
    assert result['trades'][0]['net_pnl'] is None


def test_provider_recovery_clears_failure_gate(app,monkeypatch):
    seed(app,snapshot())
    async def catalog(self):return None
    async def fail(self,metadata):raise server.ProviderError('fixture outage')
    monkeypatch.setattr(server.MarketBoard,'catalog',catalog)
    monkeypatch.setattr(server.Dhan,'refresh',fail)
    monkeypatch.setattr(server.time,'monotonic',lambda:100)
    c=client(app)
    assert c.post('/api/refresh',json={}).status_code==503
    async def recover(self,metadata):return snapshot(),candles()
    monkeypatch.setattr(server.Dhan,'refresh',recover)
    monkeypatch.setattr(server.time,'monotonic',lambda:131)
    assert c.post('/api/refresh',json={}).status_code==200
    assert c.post('/api/plans',json=plan()).json()['allowed']


def test_public_workspace_needs_no_key_and_blocks_token_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE', 'public')
    monkeypatch.setenv('THETA_API_KEY', 'old-private-key')
    monkeypatch.delenv('K_SERVICE', raising=False)
    monkeypatch.delenv('THETA_GCS_BUCKET', raising=False)
    monkeypatch.setenv('THETA_GOOGLE_CLIENT_ID','fixture-client')
    monkeypatch.setenv('THETA_ADMIN_EMAILS','owner@gmail.com')
    monkeypatch.delenv('THETA_DHAN_TOKEN_SECRET',raising=False)
    c = TestClient(server.create_app(tmp_path/'public.sqlite3'))
    status = c.get('/api/status')
    assert status.status_code == 200
    assert status.json()['auth_mode'] == 'public'
    assert status.json()['token_screen_enabled'] is False
    assert c.get('/api/datasets').status_code == 200
    assert c.post('/api/dhan/token', json={'token':'x'*40}).status_code == 403
    assert c.post('/api/refresh', json={}, headers={'Origin':'https://foreign.test'}).status_code == 403
    assert c.get('/api/status', headers={'Host':'foreign.test'}).status_code == 400
