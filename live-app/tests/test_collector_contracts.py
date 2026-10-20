import asyncio
from datetime import date
import json
import pytest
from fastapi import HTTPException
from collection import Collector,CollectionPlan,digest
from collection_contracts import chain_universe
from provider import ProviderError
from request_review import diagnose
from store import Store
from test_collection import instrument,seed,Provider,run,plan,candles


def master_items():
    return [instrument(),instrument(security_id='25',symbol='BANKNIFTY'),
        instrument(security_id='1',symbol='NON_OPTION_INDEX'),
        instrument(security_id='100',symbol='A COMPANY LTD',instrument='EQUITY',segment='NSE_EQ',raw_segment='E'),
        instrument(security_id='200',instrument='OPTSTK',segment='NSE_FNO',underlying_id='100',underlying='COMPANY',expiry='2099-01-01'),
        instrument(security_id='201',instrument='OPTIDX',segment='NSE_FNO',underlying_id='26000',underlying='NIFTY',expiry='2099-01-01'),
        instrument(security_id='202',instrument='OPTIDX',segment='NSE_FNO',underlying_id='26009',underlying='BANKNIFTY',expiry='2099-01-01'),
        instrument(security_id='203',instrument='OPTFUT',segment='MCX_COMM',exchange='MCX',underlying_id='114',underlying='GOLD',expiry='2099-01-01'),
        instrument(security_id='204',instrument='OPTCUR',segment='NSE_CURRENCY',underlying_id='20',underlying='USDINR',expiry='2024-01-01'),
        instrument(security_id='205',instrument='OPTFUT',segment=None,underlying_id='1',underlying='GOLD',reason='NSE_M unresolved',expiry='2099-01-01')]


def test_actual_index_exchange_ids_are_resolved_through_master_not_assumed():
    entries,report=chain_universe(master_items(),date(2026,10,20))
    assert {(e['segment'],e['security_id']) for e in entries}=={('IDX_I','13'),('IDX_I','25'),('NSE_EQ','100'),('MCX_COMM','114')}
    assert report['expired_option_rows_excluded']==1
    assert report['unresolved_option_rows']=={'NSE_M unresolved':1}
    assert all(e['symbol']!='NON_OPTION_INDEX' for e in entries)
    assert all(e['expiry'] is None for e in entries)


def test_new_chain_plan_contains_only_deduplicated_option_underlyings(tmp_path):
    s=Store(tmp_path/'s.db');seed(s,master_items());c=Collector(Provider(),s)
    j=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['chain']))
    assert j['chain_discovery']['underlyings']==4
    assert sum(p['count'] for p in c.get_job(j['id'])['pages'])==4
    assert j['chain_planner_version']==2


def test_old_chain_cursor_migrates_only_between_tasks_and_retains_outcomes(tmp_path):
    s=Store(tmp_path/'s.db');cat=seed(s,master_items());s.put_document('catalogue/'+cat['sha256']+'/manifest',cat,0)
    c=Collector(Provider(),s);j=c.get_job(c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['daily','chain','quotes']))['id'])
    old_pages=[{**cat['pages'][0],'family':f} for f in ['daily','chain','quotes']]
    j.update(pages=old_pages,chain_planner_version=1,items_assessed=7,rows_saved=500)
    j['cursor']={'page':1,'item':1,'variant':0,'window':0};before=dict(j['cursor'])
    j['current']={'family':'chain_snapshot'};c.upgrade_chain_job(j)
    assert j['chain_planner_version']==1 and j['cursor']==before
    j.pop('current');c.upgrade_chain_job(j)
    assert j['chain_planner_version']==2 and j['rows_saved']==500 and j['items_assessed']==7
    assert j['pages'][0]['family']=='daily' and j['pages'][-1]['family']=='quotes'
    assert j['chain_planner_upgrade']['old_cursor']==before


def test_expired_quote_rows_are_grouped_without_provider_requests(tmp_path):
    s=Store(tmp_path/'s.db');seed(s,[instrument(security_id=str(n),instrument='FUTCUR',segment='BSE_CURRENCY',expiry='2024-01-01') for n in range(500)])
    p=Provider();c=Collector(p,s);jid=plan(c) # Intraday active endpoint is ineligible too.
    assert run(c,jid)['counts']['unsupported']==1 and not p.calls
    j=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['quotes']))
    c.control(j['id'],'resume');r=run(c,j['id'])
    assert r['counts']['unsupported']==1 and not p.calls
    entry=c.results(j['id'])['items'][0]
    assert entry['result']['instruments_assessed']==500 and entry['result']['coverage']=='NOT_COLLECTED'
    assert run(c,j['id'])['state']=='completed'


def test_empty_bulk_quote_probes_are_bounded_partial_is_not_completed(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s,[instrument(security_id=str(n),symbol='NIFTY' if n==13 else str(n)) for n in range(100)])
    p=Provider();c=Collector(p,s);calls=[]
    async def call(owner,path,body):
        calls.append(body)
        ids=body['IDX_I'];return {'data':{'IDX_I':{str(ids[0]):{'last_price':42}} if len(ids)==1 else {}}}
    monkeypatch.setattr(c,'call',call)
    jid=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['quotes']))['id'];c.control(jid,'resume')
    r=run(c,jid);entry=c.results(jid)['items'][0]
    assert len(calls)==4 and calls[1]=={'IDX_I':[13]}
    assert r['counts']['partial']==1 and r['counts']['completed']==0 and r['rows_saved']==3
    assert entry['result']['isolated_probes']==3 and len(entry['result']['missing_security_ids'])==97
    raw=s.document(entry['result']['raw_key'])[0]
    assert raw['raw']=={'data':{'IDX_I':{}}} and len(raw['isolated_probes'])==3
    retry=c.retry(jid,entry['key']);task=c.get_job(retry['id'])['current']
    assert len(task['batch'])==97 and '13' not in [i['security_id'] for i in task['batch']]


def test_empty_quote_probe_token_error_stops_same_checkpoint(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s,[instrument(security_id=str(n)) for n in range(4)])
    c=Collector(Provider(),s);calls=[]
    async def call(owner,path,body):
        calls.append(body)
        if len(calls)>1:raise ProviderError('expired',code='807',status=401)
        return {'data':{'IDX_I':{}}}
    monkeypatch.setattr(c,'call',call)
    jid=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['quotes']))['id'];c.control(jid,'resume')
    before=c.get_job(jid)['cursor'];r=run(c,jid)
    assert r['state']=='token_required' and r['items_assessed']==0
    assert c.get_job(jid)['cursor']==before and len(calls)==2


def test_diagnose_failed_result_uses_original_date_bounds_and_shared_quota(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);p=Provider();c=Collector(p,s);jid=plan(c)
    j=c.get_job(jid);task=c.task(j,'daily',instrument());task.update({'from':'2026-08-01','to':'2026-08-31'})
    key='results/'+jid+'/'+digest(task);s.put_document(key,{'task':task,'result':{'status':'failed'}},0)
    calls=[]
    async def call(owner,path,body):
        assert s.document('control/lease')[0]['owner']==owner
        calls.append((path,body));return candles('2026-08-28')
    monkeypatch.setattr(c,'call',call)
    before=c.get_job(jid);r=asyncio.run(diagnose(c,jid,key))
    assert len(calls)==2 and calls[0][1]['toDate']=='2026-08-31'
    assert calls[0][1]['fromDate']=='2026-08-24' and r['checkpoint_changed'] is False
    assert c.get_job(jid)==before and s.document('control/lease')[0]['until']==0
    with pytest.raises(HTTPException):asyncio.run(diagnose(c,jid,'results/'+'b'*32+'/bad'))


def test_data_error_805_backs_off_and_does_not_advance(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);p=Provider();c=Collector(p,s);jid=plan(c)
    async def call(*args):raise ProviderError('limit',code='805',status=200)
    monkeypatch.setattr(c,'call',call);before=c.get_job(jid)['cursor'];r=run(c,jid)
    assert r['state']=='running' and r['items_assessed']==0 and r['retry_at']>0
    assert c.get_job(jid)['cursor']==before
