import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pytest
import provider as module
from provider import Dhan,ProviderError
from store import Store
from runtime import IST
from test_sdk_setup import Credentials

@pytest.fixture
def setup(tmp_path,monkeypatch):
    clock=[1000.0]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    store=Store(tmp_path/'quota.db')
    return clock,store,Dhan(Credentials(),store),Dhan(Credentials(),store)


def test_history_start_rate_is_shared_and_waits_do_not_consume_budget(setup):
    clock,s,a,b=setup;starts=[]
    for n in range(12):
        clock[0]=1000+n*.5
        assert (a if n%2 else b).reserve('charts/intraday')['granted']
        starts.append(clock[0])
        denied=b.reserve('charts/historical')
        assert not denied['granted'] and denied['at']==clock[0]+.5
        assert s.document('control/provider-budget')[0]['calls']==n+1
    assert all(sum(t<=v<t+1 for v in starts)<=2 for t in starts)


def test_quote_chain_and_history_use_distinct_intervals(setup):
    clock,s,a,b=setup
    assert a.reserve('optionchain')['granted']
    clock[0]+=.5;assert b.reserve('charts/rollingoption')['granted']
    clock[0]+=.5;assert b.reserve('marketfeed/quote')['granted']
    clock[0]+=.5
    assert a.reserve('marketfeed/ltp')=={'granted':False,'at':1002.1}
    assert a.reserve('optionchain/expirylist')=={'granted':False,'at':1003.2}
    assert a.reserve('charts/historical')['granted']
    assert s.document('control/provider-budget')[0]['calls']==4


def test_only_one_cross_instance_contender_gets_a_start(setup):
    clock,s,a,b=setup
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies=list(pool.map(lambda _:Dhan(Credentials(),s).reserve('charts/intraday'),range(8)))
    assert sum(r['granted'] for r in replies)==1
    assert s.document('control/provider-budget')[0]['calls']==1


def test_legacy_reservation_and_daily_budget_survive_upgrade(setup):
    clock,s,a,b=setup
    day=datetime.now(IST).date().isoformat()
    s.put_document('control/provider-budget',{'day':day,'calls':49999,'next_at':1003.2},0)
    assert not a.reserve('charts/historical')['granted']
    assert s.document('control/provider-budget')[0]['calls']==49999
    clock[0]=1003.2;assert b.reserve('charts/intraday')['granted']
    with pytest.raises(ProviderError) as error:a.reserve('marketfeed/quote')
    assert error.value.code=='BUDGET' and s.document('control/provider-budget')[0]['calls']==50000


def test_day_rollover_resets_count_but_not_active_spacing(setup):
    clock,s,a,b=setup
    s.put_document('control/provider-budget',{'day':'1900-01-01','calls':100000,'next_at':1000.25},0)
    assert not a.reserve('charts/historical')['granted']
    clock[0]=1000.25;assert b.reserve('charts/intraday')['granted']
    assert s.document('control/provider-budget')[0]['calls']==1


def test_shared_cooldown_blocks_all_data_families(setup):
    clock,s,a,b=setup;a.rate_backoff()
    for path in ('charts/historical','marketfeed/quote','optionchain'):
        assert b.reserve(path)=={'granted':False,'at':1060.0}
    assert s.document('control/provider-budget')[0].get('calls',0)==0
    clock[0]=1060;assert b.reserve('charts/intraday')['granted']


def test_waiting_request_rechecks_after_delayed_wake_and_competing_start(setup,monkeypatch):
    clock,s,a,b=setup
    assert a.reserve('marketfeed/quote')['granted']
    clock[0]=1000.25;sleeps=[];sent=[]
    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps)==1:
            clock[0]=1002
            assert a.reserve('marketfeed/quote')['granted']
        else:clock[0]+=seconds
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    def request(path,body):sent.append(clock[0]);return {'ok':True}
    monkeypatch.setattr(b,'_request',request)
    assert asyncio.run(b.post('marketfeed/quote',{'IDX_I':[13]}))=={'ok':True}
    assert len(sleeps)==2 and sent==[1003.1]
    assert s.document('control/provider-budget')[0]['calls']==3


@pytest.mark.parametrize('status,code,cooldown',[(429,'805',True),(400,'DH-904',True),(400,'DH-905',False),(401,'DH-901',False)])
def test_only_provider_throttling_installs_global_backoff(setup,monkeypatch,status,code,cooldown):
    clock,s,a,b=setup
    def request(path,body):raise ProviderError('redacted failure',code=code,status=status)
    monkeypatch.setattr(a,'_request',request)
    with pytest.raises(ProviderError):asyncio.run(a.post('charts/historical',{}))
    state=s.document('control/provider-budget')[0]
    assert bool(state.get('cooldown_until')) is cooldown
    assert state['calls']==1


def test_old_worker_cannot_bypass_new_chain_spacing_during_rollout(setup):
    clock,s,a,b=setup
    assert a.reserve('optionchain')['granted']
    clock[0]+=.5;assert b.reserve('charts/historical')['granted']
    state,version=s.document('control/provider-budget')
    # An older image knows only next_at and schedules its call after that gate.
    legacy_at=max(clock[0],state['next_at'])
    assert legacy_at>=1003.2
    assert s.put_document('control/provider-budget',{'day':state['day'],'calls':state['calls']+1,'next_at':legacy_at+3.2},version)
    assert not a.reserve('optionchain')['granted']
    assert a.reserve('charts/intraday')['at']==legacy_at+3.2
    clock[0]=legacy_at+3.2;assert a.reserve('charts/intraday')['granted']


def test_public_pacing_status_exposes_counts_without_private_ledger_fields(setup):
    from data_system import status
    clock,s,a,b=setup
    assert a.reserve('charts/historical')['granted']
    state,version=s.document('control/provider-budget')
    s.put_document('control/provider-budget',{**state,'private_value':'must-not-appear'},version)
    report=status(s)['request_pacing']
    assert report['policy']=='endpoint-starts-v1' and report['attempts_reserved']==1
    assert report['daily_guard']==50000 and 'private_value' not in report


def test_new_parallel_guard_does_not_reset_existing_account_usage(setup):
    clock,s,a,b=setup
    s.put_document('control/provider-budget',{'day':datetime.now(IST).date().isoformat(),'calls':60000},0)
    with pytest.raises(ProviderError):
        a.reserve('charts/historical')
    assert s.document('control/provider-budget')[0]['calls']==60000


def test_account_budget_endpoint_only_approves_the_activated_worker(tmp_path):
    from fastapi.testclient import TestClient
    from server import create_app
    from build_identity import runtime_build
    app=create_app(tmp_path/'account.db')
    with TestClient(app) as client:
        report=client.get('/api/collection/account-budget').json()
        assert report['legacy_daily_guard']==50000 and not report['worker_release_matches']
        app.state.store.put_document('control/worker',{'build_id':runtime_build()},0)
        assert client.get('/api/collection/account-budget').json()['worker_release_matches'] is True
