import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from collection import Collector
from instrument_directory import InstrumentDirectory
from provider import ProviderError
from store import Store

SHA='a'*64

def item(key='index', **kw):
    return {'key':key,'security_id':'13','exchange':'NSE','raw_segment':'I','segment':'IDX_I',
            'instrument':'INDEX','symbol':'NIFTY','display':'Nifty','expiry':None,'reason':None,**kw}

def seed(collector, items=None, sha=SHA):
    entries=items if items is not None else [item()]
    pages=[]
    for n in range(0,len(entries),3):
        key=f'catalogue/{sha}/pages/{n}'
        collector.store.put_document(key,{'items':entries[n:n+3]},0)
        pages.append({'key':key,'group':'IDX_I-INDEX','count':len(entries[n:n+3])})
    cat={'sha256':sha,'fetched_at':datetime.now(timezone.utc).isoformat(),'rows':len(entries),'pages':pages}
    collector.store.put_document('catalogue/current',cat,collector.store.document('catalogue/current')[1])
    return cat

@pytest.fixture
def directory(tmp_path):
    collector=Collector(SimpleNamespace(),Store(tmp_path/'db.sqlite3'));seed(collector)
    return InstrumentDirectory(collector)

def run(coro):return asyncio.run(coro)

def test_whole_master_pages_filters_literals_and_review_reasons(directory):
    entries=[item(str(n),symbol=f'STOCK{n}',segment='BSE_EQ',instrument='EQUITY',expiry='1900-01-01') for n in range(55)]
    entries += [item('literal',symbol='ABC%_'),item('unknown',symbol='GOLD',segment=None,reason='No documented mapping: NSE_M'),
                item('expired',symbol='GOLD',instrument='FUTCOM',expiry='2000-01-01',segment='MCX_COMM')]
    seed(directory.collector,entries,'b'*64)
    data=directory.search(segment='BSE_EQ')
    assert data['matches']==55 and len(data['items'])==50 and data['next_page']==1
    assert len(directory.search(segment='BSE_EQ',page=1)['items'])==5
    assert directory.search('%_')['items'][0]['key']=='literal'
    assert directory.search('GOLD')['matches']==1
    assert directory.search('GOLD',active_only=False)['matches']==2
    assert directory.search(segment='UNMAPPED')['items'][0]['reason']
    for args in [('','NSE_M'),('x'*81,'')]:
        with pytest.raises(HTTPException):directory.search(*args)

def test_stale_selection_unresolved_and_expired_contracts_are_not_queried(directory):
    seed(directory.collector,[item(),item('bad',segment=None,reason='No documented mapping'),
         item('old',instrument='FUTIDX',expiry='2000-01-01')],'b'*64)
    for key,sha,current in [('index',SHA,False),('bad','b'*64,False),('old','b'*64,True)]:
        with pytest.raises(HTTPException) as e:directory.resolve(key,sha,current)
        assert e.value.status_code==409
    assert directory.resolve('old','b'*64)['expiry']=='2000-01-01'
    cat=directory.collector.catalogue();cat['fetched_at']='2000-01-01T00:00:00+00:00'
    directory.collector.store.put_document('catalogue/current',cat,directory.collector.store.document('catalogue/current')[1])
    with pytest.raises(HTTPException) as e:directory.resolve('index','b'*64,True)
    assert e.value.status_code==409

@pytest.mark.parametrize('segment', ['IDX_I','NSE_EQ','BSE_EQ','NSE_FNO','BSE_FNO','NSE_CURRENCY','BSE_CURRENCY','MCX_COMM'])
def test_quote_keeps_exact_segment_and_security_id_and_releases_lease(directory,segment):
    seed(directory.collector,[item('exact',segment=segment)],'b'*64)
    calls=[]
    async def call(owner,path,body):
        calls.append((path,body))
        return {'data':{segment:{'13':{'last_price':123,'last_trade_time':(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(),'volume':44,'oi':66}}}}
    directory.collector.call=call
    data=run(directory.quote('exact','b'*64))
    assert calls==[('marketfeed/quote',{segment:[13]})]
    assert data['segment']==segment and data['observation']['volume']==44
    assert data['status']=='TIME_UNAVAILABLE'
    assert directory.collector.store.rows('instrument_quote')[0]['data']['raw']==data['raw']
    assert directory.collector.store.document('control/lease')[0]['until']==0

def test_quote_provider_rejection_and_malformed_response_do_not_invent_price(directory):
    async def fail(*args):raise ProviderError('Dhan rejected request',code='DH-907')
    directory.collector.call=fail
    with pytest.raises(ProviderError):run(directory.quote('index',SHA))
    assert directory.collector.store.document('control/lease')[0]['until']==0
    async def malformed(*args):return {'data':[]}
    directory.collector.call=malformed
    assert run(directory.quote('index',SHA))['status']=='NO_QUOTE'

def test_history_uses_selected_identity_validates_and_preserves_rejected_raw(directory):
    calls=[]
    stamp=int(datetime(2026,10,22,4,0,tzinfo=timezone.utc).timestamp())
    raw={'timestamp':[stamp],'open':[100],'high':[102],'low':[99],'close':[101],'volume':[20]}
    async def call(owner,path,body):calls.append((path,body));return raw
    directory.collector.call=call
    result=run(directory.history('index',SHA,date(2026,10,22),date(2026,10,23)))
    assert result['quality']['rows']==1 and result['request']['securityId']=='13'
    assert calls[0][0]=='charts/intraday' and calls[0][1]['interval']=='1'
    assert directory.collector.store.document('candles/'+result['quality']['series']+'/2026-10-22')[0]
    raw['high']=[10]
    with pytest.raises(ProviderError):run(directory.history('index',SHA,date(2026,10,22),date(2026,10,23)))
    assert len(directory.collector.store.rows('instrument_history_source'))==2
    assert directory.collector.store.document('control/lease')[0]['until']==0
    raw['high']=[102]
    run(directory.history('index',SHA,date(2026,10,22),date(2026,10,23),'daily'))
    assert calls[-1][0]=='charts/historical' and calls[-1][1]['expiryCode']==0 and 'interval' not in calls[-1][1]

@pytest.mark.parametrize('start,end,family,interval',[
    (date(2026,10,23),date(2026,10,23),'intraday',1),
    (date(2026,1,1),date(2026,10,23),'intraday',1),
    (date(2026,10,22),date(2026,10,23),'intraday',3),
    (date(2026,10,22),date(2099,1,1),'daily',1)])
def test_invalid_history_bounds_make_no_provider_request(directory,start,end,family,interval):
    with pytest.raises(HTTPException) as e:run(directory.history('index',SHA,start,end,family,interval))
    assert e.value.status_code==422
    assert directory.collector.store.document('control/lease')[0] is None

def test_routes_require_admin_before_provider_access(tmp_path,monkeypatch):
    import server
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    monkeypatch.delenv('K_SERVICE',raising=False);monkeypatch.delenv('THETA_GCS_BUCKET',raising=False)
    app=server.create_app(tmp_path/'app.sqlite3');seed(app.state.collector)
    def admin(request):
        if request.headers.get('x-theta-admin-password')!='test':raise HTTPException(403,'Locked')
    monkeypatch.setattr(server,'require_admin',admin)
    client=TestClient(app);body={'key':'index','catalogue_sha256':SHA}
    assert client.get('/api/instruments?q=NIFTY').json()['items'][0]['key']=='index'
    for path,payload in [('quote',body),('history',{**body,'start':'2026-10-22','end':'2026-10-23'})]:
        assert client.post('/api/instruments/'+path,json=payload).status_code==403
    async def call(*args):return {'data':{'IDX_I':{'13':{'last_price':100}}}}
    app.state.collector.call=call
    assert client.post('/api/instruments/quote',json=body,headers={'x-theta-admin-password':'test'}).json()['price']==100
    assert client.post('/api/instruments/quote',json={**body,'segment':'NSE_EQ'},headers={'x-theta-admin-password':'test'}).status_code==422
    assert client.post('/api/instruments/quote',json=body,headers={'Origin':'https://hostile.example','x-theta-admin-password':'test'}).status_code==403

def test_recorded_data_matches_segment_not_just_security_id_and_exposes_receipt_age(directory):
    seed(directory.collector,[item('equity',segment='BSE_EQ')],'b'*64)
    store=directory.collector.store
    assert directory.recorded('equity','b'*64)['state']=='NOT_SUBSCRIBED'
    store.put_document('system/subscription-plan',{'revision':'new','scopes':{'live-3':[item('equity',segment='BSE_EQ')]}},0)
    assert directory.recorded('equity','b'*64)['state']=='AWAITING_OBSERVATION'
    at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    store.put_document('system/latest/live-3',{'rows':{'1:13':{'segment':'NSE_EQ','security_id':'13','received_at':at,'ltp':1},
         '4:13':{'segment':'BSE_EQ','security_id':'13','received_at':at,'ltp':200}}},0)
    result=directory.recorded('equity','b'*64)
    assert result['observation']['ltp']==200 and result['receipt_age_seconds']>=300
    assert result['state']=='RETAINED_OBSERVATION' and result['recorder']=='live-3'

def test_master_replacement_keeps_open_readers_valid_and_bounds_disk_indexes(directory):
    with directory.database() as (db,cat):
        seed(directory.collector,[item('new')],'b'*64)
        assert directory.search()['items'][0]['key']=='new'
        assert db.execute('SELECT key FROM instruments').fetchone()[0]=='index'
    assert len(list(__import__('pathlib').Path(directory.temporary.name).glob('*.sqlite3')))==1
