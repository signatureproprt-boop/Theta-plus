import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from collection import Collector, CollectionPlan, parse_catalogue, digest
from provider import ProviderError
from store import Store
import server

def instrument(**kw):
    return dict(key='abc',security_id='13',exchange='NSE',raw_segment='I',segment='IDX_I',
                instrument='INDEX',symbol='NIFTY',display='Nifty',expiry=None,
                underlying='',underlying_id='',strike=None,side=None,expiry_flag=None,
                lot_size=None,tick_size=None,reason=None,**kw) if not kw else {**instrument(),**kw}

def seed(store,entries=None,group='IDX_I-INDEX'):
    entries=entries or [instrument()]
    page='catalogue/'+'a'*64+'/pages/00000'
    store.put_document(page,{'items':entries},0)
    cat={'sha256':'a'*64,'rows':len(entries),'fetched_at':'now','segments':{},'review_reasons':{},
         'pages':[{'key':page,'group':group,'count':len(entries)}]}
    store.put_document('catalogue/current',cat,0)
    return cat

class Provider:
    configured=True
    def __init__(self):
        self.calls=[]
        self.problem=None
        self.answer=None
    async def post(self,path,body):
        self.calls.append((path,body))
        if self.problem:raise self.problem
        return self.answer if self.answer is not None else candles()

def candles(day='2026-10-16', extra=None):
    stamp=datetime.fromisoformat(day+'T09:16:00').replace(tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()
    return {**dict(timestamp=[stamp],open=[10],high=[12],low=[9],close=[11],volume=[100]),**(extra or {})}

@pytest.fixture
def setup(tmp_path,monkeypatch):
    store=Store(tmp_path/'collector.db')
    seed(store)
    provider=Provider()
    collector=Collector(provider,store)
    # Other tests exercise the shared reservation independently. Avoid actual sleeps.
    async def call(owner,path,body):return await provider.post(path,body)
    monkeypatch.setattr(collector,'call',call)
    return collector,provider,store

def plan(collector,**kw):
    job=collector.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['intraday'],**kw))
    collector.control(job['id'],'resume')
    return job['id']

def run(c,jid):return asyncio.run(c.run(jid))

def test_catalogue_preserves_unmapped_and_equity_blank_expiry():
    text='EXCH_ID,SEGMENT,SECURITY_ID,SYMBOL_NAME,INSTRUMENT,SM_EXPIRY_DATE\nNSE,E,123,ABC,EQUITY,\nNSE,M,42,GOLD,FUTCOM,2026-12-01\nNSE,I,13,NIFTY,INDEX,\n'
    entries,counts,reasons=parse_catalogue(text)
    assert len(entries)==3
    assert entries[0]['segment']=='NSE_EQ' and entries[0]['reason'] is None
    assert entries[1]['segment'] is None and entries[1]['reason']
    assert counts['NSE_M']==1 and sum(reasons.values())==1

def test_sqlite_cas_single_winner_and_independent_reads(tmp_path):
    s=Store(tmp_path/'store.db')
    assert s.put_document('control/test',{'x':0},0)
    value,version=s.document('control/test')
    with ThreadPoolExecutor(8) as pool:
        results=list(pool.map(lambda n:s.put_document('control/test',{'x':n},version),range(8)))
    assert sum(results)==1
    value['x']=999
    assert s.document('control/test')[0]['x']!=999

def test_shared_lease_blocks_other_instances_and_recovers(tmp_path):
    s=Store(tmp_path/'lease.db');a=Collector(Provider(),s);b=Collector(Provider(),s)
    owner=a.acquire()
    with pytest.raises(HTTPException) as exc:b.acquire()
    assert exc.value.status_code==409
    a.release(owner)
    owner2=b.acquire()
    a.release(owner)
    assert s.document('control/lease')[0]['owner']==owner2
    b.release(owner2)

def test_resume_restart_idempotent_raw_and_normalized(setup,monkeypatch):
    c,p,s=setup
    jid=plan(c)
    result=run(c,jid)
    assert result['counts']['completed']==1 and result['rows_saved']==1
    assert len(p.calls)==1
    assert run(c,jid)['state']=='completed'
    # A new process/job reuses the persisted raw response.
    other=Collector(p,s)
    jid2=plan(other)
    assert run(other,jid2)['counts']['completed']==1
    assert len(p.calls)==1
    names=s.document_names('candles/')
    assert len(names)==1 and len(s.document(names[0])[0]['candles'])==1

def test_overlapping_ranges_merge_same_candle(setup):
    c,p,s=setup
    run(c,plan(c))
    second=c.create(CollectionPlan(start='2026-10-15',end='2026-10-17',families=['intraday']))
    c.control(second['id'],'resume');run(c,second['id'])
    names=s.document_names('candles/')
    assert len(names)==1 and len(s.document(names[0])[0]['candles'])==1

def test_token_expiry_preserves_cursor_then_resume(setup):
    c,p,s=setup;jid=plan(c);p.problem=ProviderError('Expired',code='807')
    result=run(c,jid)
    assert result['state']=='token_required' and result['items_assessed']==0
    checkpoint=c.get_job(jid)['current']
    p.problem=None;c.control(jid,'resume')
    assert run(c,jid)['counts']['completed']==1
    assert checkpoint['from']=='2026-10-16'

def test_permissions_are_explicit_and_can_continue(setup):
    c,p,s=setup;jid=plan(c);p.problem=ProviderError('No entitlement',code='806')
    assert run(c,jid)['state']=='permission_blocked'
    c.control(jid,'skip-blocked')
    assert c.results(jid)['items'][0]['result']['status']=='permission_blocked'
    c.control(jid,'resume')
    assert run(c,jid)['state']=='completed'
    assert len(p.calls)==1

def test_pause_wins_over_inflight_result(setup):
    c,p,s=setup;jid=plan(c)
    async def during(owner,path,body):
        c.control(jid,'pause')
        return candles()
    c.call=during
    assert run(c,jid)['state']=='paused'
    assert c.get_job(jid)['counts']['completed']==1
    assert len(c.results(jid)['items'])==1

def test_empty_is_explicit_and_retry_refetches(setup):
    c,p,s=setup;jid=plan(c);p.answer={k:[] for k in candles()}
    assert run(c,jid)['counts']['empty']==1
    entry=c.results(jid)['items'][0]
    retry=c.retry(jid,entry['key'])
    p.answer=None;c.control(retry['id'],'resume')
    assert run(c,retry['id'])['counts']['completed']==1
    assert len(p.calls)==2

def test_bad_arrays_record_failure_without_inventing_candles(setup):
    c,p,s=setup;jid=plan(c);p.answer=candles(extra={'volume':[]})
    for _ in range(3):
        job=c.get_job(jid);job['retry_at']=0;c.save_job(job);result=run(c,jid)
    assert result['counts']['failed']==1
    assert c.results(jid)['items'][0]['result']['status']=='failed'
    assert s.document_names('candles/')==[]

def test_ninety_day_intraday_thirty_day_rolling_and_strike_limits(setup):
    c,p,s=setup
    job=c.get_job(plan(c))
    job['config'].update(start='2026-01-01',end='2026-10-17')
    t=c.task(job,'intraday',instrument())
    assert t['from']=='2026-01-01' and t['to']=='2026-04-01'
    item=instrument(instrument='OPTIDX',segment='NSE_FNO',expiry_flags=['MONTH'])
    variants=c.variants('rolling',item,job)
    assert len([v for v in variants if v['expiryCode']==0 and v['expiryFlag']=='MONTH'])==42
    assert len([v for v in variants if v['expiryCode']==1 and v['expiryFlag']=='MONTH'])==14
    t=c.task(job,'rolling',item)
    assert t['to']=='2026-01-31'
    stock=instrument(instrument='OPTSTK',expiry_flags=['MONTH'])
    assert len(c.variants('rolling',stock,job))==42

def test_horizon_gap_recorded_before_supported_range(setup):
    c,p,s=setup;jid=plan(c);job=c.get_job(jid)
    job['config']['start']='1900-01-01';c.save_job(job)
    result=run(c,jid)
    assert result['counts']['unsupported']==1 and len(p.calls)==0
    run(c,jid)
    assert p.calls[0][1]['fromDate']>'2020-01-01'

def test_chain_all_expiries_checkpointed_and_raw_exportable(setup):
    c,p,s=setup
    job=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['chain']))
    jid=job['id'];c.control(jid,'resume')
    p.answer={'data':['2099-10-01','2099-11-01']}
    assert run(c,jid)['counts']['completed']==1
    p.answer={'data':{'oc':{'100':{'ce':{}}}}}
    run(c,jid);run(c,jid)
    assert [b['Expiry'] for path,b in p.calls if path=='optionchain']==['2099-10-01','2099-11-01']
    assert run(c,jid)['state']=='completed'
    assert c.get_job(jid)['counts']['completed']==3

def test_quote_batch_reports_individual_omissions(tmp_path,monkeypatch):
    s=Store(tmp_path/'quote.db')
    seed(s,[instrument(security_id=str(i),key=str(i)) for i in range(1002)])
    p=Provider();c=Collector(p,s)
    async def call(owner,path,body):
        assert len(body['IDX_I'])<=1000
        return {'data':{'IDX_I':{'0':{'last_price':42}}}}
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['quotes']))
    jid=job['id'];c.control(jid,'resume');run(c,jid)
    entry=c.results(jid)['items'][0]
    assert entry['result']['expected']==1000 and len(entry['result']['missing_security_ids'])==999
    assert c.get_job(jid)['cursor']['item']==1000

def test_normalized_duplicate_times_and_ohlc_validation(setup):
    c,p,s=setup;jid=plan(c)
    raw=candles()
    p.answer={k:v+v for k,v in raw.items()}
    result=run(c,jid)
    assert result['rows_saved']==1
    assert c.results(jid)['items'][0]['result']['duplicate_timestamps']==1

def test_result_pagination_covers_more_than_fifty(setup):
    c,p,s=setup;jid=plan(c)
    for i in range(65):s.put_document('results/'+jid+'/'+str(i).zfill(64),{'result':{'status':'unsupported'}},0)
    first=c.results(jid);second=c.results(jid,first['next_cursor'])
    assert len(first['items'])==50 and len(second['items'])==15
    assert not second['next_cursor']

def test_api_mutations_require_existing_admin(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('K_SERVICE','THETA_GCS_BUCKET','THETA_TOKEN_ADMIN_HASH'):
        monkeypatch.delenv(key,raising=False)
    app=server.create_app(tmp_path/'api.db');client=TestClient(app)
    for path,body in [('catalogue/refresh',{}),('profile/check',{}),
        ('jobs',{'start':'2026-10-16','end':'2026-10-17'}),('jobs/'+'a'*32+'/run',{}),('retry',{})]:
        assert client.post('/api/collection/'+path,json=body).status_code==503
    assert client.get('/api/collection/catalogue').json()=={}
    assert client.get('/api/collection/export/control/lease').status_code==422
    assert client.get('/api/collection/jobs/not-a-job').status_code==404
    assert 'Collect all data' in client.get('/').text

def test_daily_quota_is_reserved_across_instances(tmp_path):
    from collection import now
    s=Store(tmp_path/'quota.db');c=Collector(Provider(),s);owner=c.acquire()
    lease,version=s.document('control/lease')
    lease.update(day=datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(),calls=100000)
    s.put_document('control/lease',lease,version)
    with pytest.raises(ProviderError) as exc:asyncio.run(c.call(owner,'marketfeed/quote',{}))
    assert exc.value.code=='BUDGET'
    c.release(owner)

def test_gcs_documents_use_generation_cas_and_bypass_record_cache():
    from types import SimpleNamespace
    from cloud_store import CloudStore
    from google.api_core.exceptions import NotFound, PreconditionFailed
    data={}
    class Blob:
        def __init__(self,name):self.name=name;self.generation=None
        def reload(self,**kw):
            if self.name not in data:raise NotFound('missing')
            self.generation=data[self.name][0]
        def upload_from_string(self,text,if_generation_match,**kw):
            old=data.get(self.name,(0,None))
            if if_generation_match!=old[0]:raise PreconditionFailed('stale')
            data[self.name]=(old[0]+1,text)
        def download_as_text(self,if_generation_match,**kw):
            old=data.get(self.name)
            if not old:raise NotFound('missing')
            if old[0]!=if_generation_match:raise PreconditionFailed('stale')
            return old[1]
    class Bucket:
        def blob(self,name):return Blob(name)
        def list_blobs(self,prefix,max_results,start_offset=''):
            return [SimpleNamespace(name=n) for n in sorted(data) if n.startswith(prefix) and n>=start_offset][:max_results]
    client=SimpleNamespace(bucket=lambda name:Bucket())
    s=CloudStore('private',client=client)
    assert s.document('jobs/abc')==(None,0)
    assert s.put_document('jobs/abc',{'state':'paused'},0)
    old,version=s.document('jobs/abc')
    assert not s.put_document('jobs/abc',{'state':'wrong'},0)
    assert s.put_document('jobs/abc',{'state':'running'},version)
    assert s.document('jobs/abc')[0]['state']=='running'
    assert not s.put_document('jobs/abc',old,version)
    for k in ('def','ghi'):s.put_document('jobs/'+k,{},0)
    assert s.document_names('jobs/',after='jobs/abc',limit=1)==['jobs/def']

def test_unknown_mapping_does_not_call_provider(setup):
    c,p,s=setup;jid=plan(c)
    job=c.get_job(jid)
    page=job['pages'][0]['key']
    value,v=s.document(page);value['items'][0].update(segment=None,reason='No documented mapping')
    s.put_document(page,value,v)
    assert run(c,jid)['counts']['unsupported']==1
    assert p.calls==[]

def test_failed_chain_retry_reuses_its_expiry_not_entire_parent(setup):
    c,p,s=setup
    job=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['chain']))
    jid=job['id'];c.control(jid,'resume');p.answer={'data':['2099-01-01']}
    run(c,jid);p.problem=ProviderError('Temporarily unavailable')
    for _ in range(3):
        j=c.get_job(jid);j['retry_at']=0;c.save_job(j);run(c,jid)
    entry=next(e for e in c.results(jid)['items'] if e['result']['status']=='failed')
    retry=c.retry(jid,entry['key']);p.problem=None;p.answer={'data':{'oc':{'1':{}}}}
    c.control(retry['id'],'resume')
    assert run(c,retry['id'])['counts']['completed']==1
    assert run(c,retry['id'])['state']=='completed'

def test_catalogue_refresh_streams_and_publishes_only_complete_snapshot(tmp_path,monkeypatch):
    import httpx
    original=httpx.AsyncClient
    csv_text='EXCH_ID,SEGMENT,SECURITY_ID,SYMBOL_NAME,INSTRUMENT,SM_EXPIRY_DATE,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,EXPIRY_FLAG\nNSE,I,13,NIFTY,INDEX,,,,\nNSE,D,123,NIFTY,OPTIDX,2099-01-01,13,NIFTY,W\n'
    transport=httpx.MockTransport(lambda request:httpx.Response(200,text=csv_text))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=transport,**kwargs))
    s=Store(tmp_path/'refresh.db');c=Collector(Provider(),s)
    manifest=asyncio.run(c.refresh_catalogue())
    assert manifest['rows']==2
    assert s.document('catalogue/current')[0]['sha256']==manifest['sha256']
    assert (tmp_path/'catalogue-sources'/ (manifest['sha256']+'.csv')).read_text()==csv_text
    rolling=next(p for p in manifest['pages'] if p['group']=='rolling-underlyings')
    assert s.document(rolling['key'])[0]['items'][0]['security_id']=='13'
    assert not s.document('control/lease')[0]['until']

def test_live_capture_reports_missing_subscribed_instruments(setup,monkeypatch):
    import struct
    import websockets
    c,p,s=setup
    from types import SimpleNamespace
    p.credentials=SimpleNamespace(token=lambda:'fixture-token',client_id='fixture-client')
    packet=struct.pack('<BHBI',1,8,0,13)
    class Socket:
        def __init__(self):self.done=False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,body):self.body=json.loads(body)
        async def recv(self):
            if self.done:raise asyncio.TimeoutError()
            self.done=True;return packet
    socket=Socket()
    monkeypatch.setattr(websockets,'connect',lambda *a,**kw:socket)
    task={'family':'live','item':instrument(),'batch':[instrument(),instrument(security_id='25')]}
    response=asyncio.run(c.capture(task));result=c.persist_result(task,response)
    assert socket.body['InstrumentCount']==2
    assert result['missing_security_ids']==['25']
    assert result['rows']==1 and not result['exchange_time_verified']
    assert 'fixture-token' not in json.dumps(response)

def test_off_grid_timestamp_is_not_silently_normalized(setup):
    c,p,s=setup
    task=c.task(c.get_job(plan(c)),'intraday',instrument())
    raw=candles();raw['timestamp'][0]+=1
    with pytest.raises(ProviderError,match='minute boundary'):
        c.persist_candles(task,{'raw':raw})
