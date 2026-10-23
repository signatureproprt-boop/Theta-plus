import asyncio
import copy
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest
from store import Store
from provider import ProviderError
from data_validator import report,candles,chain,gate
from build_identity import runtime_build
from session_chart import intraday_window

CLOCK=datetime(2026,10,23,3,48,tzinfo=timezone.utc)
IDENTITY={'key':'nifty','security_id':'13','segment':'IDX_I','instrument':'INDEX'}

def snapshot():
    stamps=[int(CLOCK.timestamp())-180+i*60 for i in range(3)]
    return {'symbol':'nifty','security_id':'13','session_date':'2026-10-23','received_at':CLOCK.isoformat(),
            'raw':{'timestamp':stamps,**{k:[100]*3 for k in ('open','high','low','close','volume')}}}

def chain_record():
    value={'last_price':10,'oi':100,'volume':200,'top_bid_price':9,'top_ask_price':10}
    return {'metadata':{'index':'nifty','expiry':'2026-10-27','fetched_at':CLOCK.isoformat(),'exchange_at':(CLOCK-timedelta(seconds=2)).isoformat()},
            'raw':{'data':{'oc':{'22000':{'ce':dict(value),'pe':dict(value)}}}}}

def test_current_grid_checks_all_elapsed_minutes_and_excludes_forming_bar():
    record=snapshot();raw=record['raw']
    for k,v in raw.items():v.append(CLOCK.timestamp() if k=='timestamp' else 100)
    result=candles(record,'nifty',IDENTITY,CLOCK)
    assert result['state']=='PASS' and result['expected_minutes']==3 and result['excluded_rows']==1
    for v in raw.values():v.pop(1)
    assert candles(record,'nifty',IDENTITY,CLOCK)['missing_minutes']==1

@pytest.mark.parametrize('field,value',[('close',float('nan')),('volume',-1),('open',0),('high',99)])
def test_bad_ohlcv_fail_closed(field,value):
    record=snapshot();record['raw'][field][0]=value
    assert candles(record,'nifty',IDENTITY,CLOCK)['state']=='BLOCKED'

def test_duplicate_conflicting_order_identity_and_future_times():
    record=snapshot()
    for values in record['raw'].values():values.insert(0,values[0])
    result=candles(record,'nifty',IDENTITY,CLOCK)
    assert result['state']=='WARN' and result['duplicate_rows']==1
    record['raw']['close'][0]=99;record['raw']['low'][0]=99
    assert candles(record,'nifty',IDENTITY,CLOCK)['conflicting_minutes']==1
    record=snapshot();record['security_id']='25'
    assert candles(record,'nifty',IDENTITY,CLOCK)['state']=='BLOCKED'
    record=snapshot();record['received_at']=(CLOCK+timedelta(seconds=1)).isoformat()
    assert candles(record,'nifty',IDENTITY,CLOCK)['state']=='BLOCKED'
    record=snapshot()
    for v in record['raw'].values():v.reverse()
    assert candles(record,'nifty',IDENTITY,CLOCK)['out_of_order_rows']==2

def test_missing_exchange_quote_time_never_passes_on_recent_receipt():
    record=chain_record();del record['metadata']['exchange_at']
    result=chain(record,'nifty',CLOCK)
    assert result[0]['state']=='PASS' and result[1]['state']=='BLOCKED'
    record['raw']['data']['oc']['22000']['ce']['top_bid_price']=11
    assert chain(record,'nifty',CLOCK)[0]['crossed_quotes']==1

def test_validator_reads_bounded_documents_without_mutating_archives(tmp_path):
    store=Store(tmp_path/'s.db')
    store.put_document('control/worker',{'until':CLOCK.timestamp()+100,'heartbeat':CLOCK.isoformat(),'build_id':runtime_build()},0)
    store.put_document('control/data-system',{'enabled':True},0)
    scopes={f:[IDENTITY] for f in ('live','live-extra','depth20','depth200')}
    future_identity={**IDENTITY,'key':'nifty_future','expiry':'2026-10-27'}
    scopes['live']=[IDENTITY,future_identity]
    store.put_document('system/subscription-plan',{'revision':'test','scopes':scopes},0)
    for f in scopes:
        store.put_document('system/recorder/'+f,{'state':'RECORDING','heartbeat':CLOCK.isoformat(),'last_durable_received_at':CLOCK.isoformat(),
          'subscription_revision':'test','subscribed':len(scopes[f]),'missing_since_connection':0,'decode_errors':0},0)
    store.put_document('research/session/nifty',snapshot(),0)
    store.put_document('research/future/nifty',{**snapshot(),'expiry':'2026-10-27'},0)
    store.put_document('research/current-chain/nifty',chain_record(),0)
    before=store.document('research/session/nifty')
    assert report(store,CLOCK,'nifty')['data_eligible']
    assert store.document('research/session/nifty')==before
    result=report(store,CLOCK+timedelta(minutes=3),'nifty')
    assert not result['data_eligible'] and result['blocked_checks']>=6
    setups=[{'state':'CONFIRM','missing':[],'reason_codes':[]}]
    assert gate(setups,result)[0]['state']=='DATA_UNAVAILABLE'
    assert setups[0]['state']=='CONFIRM'

def test_only_parameter_or_no_data_errors_get_one_wider_probe():
    body={'fromDate':'2026-10-23 09:14:00','toDate':'2026-10-23 09:18:00','securityId':'13'}
    calls=[]
    class Provider:
        async def post(self,path,payload):
            calls.append(copy.deepcopy(payload))
            if len(calls)==1:raise ProviderError('Rejected',code='DH-905')
            return snapshot()['raw']
    raw,actual,attempts=asyncio.run(intraday_window(Provider(),body))
    assert len(calls)==2 and actual['fromDate']=='2026-10-22 00:00:00'
    assert body['fromDate']=='2026-10-23 09:14:00' and attempts[0]['code']=='DH-905'
    assert raw==snapshot()['raw']
    calls.clear()
    class Auth:
        async def post(self,*args):calls.append(1);raise ProviderError('Unauthorized',code='DH-901')
    with pytest.raises(ProviderError):asyncio.run(intraday_window(Auth(),body))
    assert len(calls)==1

def test_current_chain_and_other_symbols_continue_after_session_failure(tmp_path,monkeypatch):
    import session_chart,dhan_data
    from data_system import refresh_current
    seen=[];failed=[]
    async def session(*args):seen.append(('session',args[-1]));raise ProviderError('Rejected',code='DH-905')
    async def chain_fetch(*args):seen.append(('chain',args[-2]))
    async def context(*args):seen.append(('context',args[-1]))
    async def failure(stage,error):failed.append(stage)
    monkeypatch.setattr(session_chart,'refresh_session',session)
    monkeypatch.setattr(session_chart,'refresh_context',context)
    monkeypatch.setattr(dhan_data,'fetch_chain',chain_fetch)
    collector=SimpleNamespace(provider=None,store=Store(tmp_path/'s.db'))
    board=SimpleNamespace(instruments={'nifty':IDENTITY,'banknifty':{**IDENTITY,'security_id':'25'}})
    asyncio.run(refresh_current(collector,board,failure))
    assert ('chain','nifty') in seen and ('chain','banknifty') in seen
    assert ('context','banknifty') in seen and ('session','india_vix') in seen
    assert len(failed)==3

def test_validator_endpoint_is_read_only_and_rejects_unknown_symbol(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('THETA_GCS_BUCKET','K_SERVICE'):monkeypatch.delenv(key,raising=False)
    client=TestClient(server.create_app(tmp_path/'app.db'))
    result=client.get('/api/data-validator?symbol=nifty')
    assert result.status_code==200 and result.json()['state']=='BLOCKED'
    assert client.get('/api/data-validator?symbol=unknown').status_code==422

@pytest.mark.parametrize('raw',[None,[],{'data':[]},{'data':{'oc':{'x':None}}}])
def test_malformed_chain_is_a_blocked_report_not_an_exception(raw):
    record=chain_record();record['raw']=raw
    assert chain(record,'nifty',CLOCK)[0]['state']=='BLOCKED'

def test_live_validation_clock_is_captured_after_concurrent_reads(tmp_path,monkeypatch):
    import data_validator
    store=Store(tmp_path/'s.db');reads=[]
    class Reading:
        def document(self,key):reads.append(key);return store.document(key)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            assert len(reads)==12  # worker/control/plan, six feeds, session/future/chain
            return CLOCK
    monkeypatch.setattr(data_validator,'datetime',Clock)
    assert report(Reading(),symbol='nifty')['checked_at']==CLOCK.isoformat()
