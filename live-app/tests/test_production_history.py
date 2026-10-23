import asyncio
from datetime import datetime,timezone,timedelta

from collection import Collector,CollectionPlan,replace,digest
from data_system import schedule_history
from gap_recovery import eligible,tick
from history_grid import requested_grid,cache_is_closed
from store import Store
from test_collection import seed,instrument,Provider,candles


def task(family='intraday',start='2026-10-21',end='2026-10-23',**item):
    return {'family':family,'item':instrument(**item),'variant':{} if family=='daily' else {'interval':'1'},'from':start,'to':end}


def full_day(day='2026-10-21',skip=()):
    raw=candles(day);first=raw['timestamp'][0]-60
    return {str(first+i*60):{'timestamp':first+i*60} for i in range(375) if i not in skip}


def test_entire_absent_session_is_a_gap_even_when_first_day_is_complete():
    q=requested_grid(task(),{'2026-10-21':full_day()})
    assert q['missing_sessions']==1 and q['missing_session_dates']==['2026-10-22']
    assert q['missing_minutes']==375 and not q['session_grid_complete']
    assert q['coverage']=='PARTIAL_SESSION_GRID'
    assert requested_grid(task(),{'2026-10-21':full_day(),'2026-10-22':full_day('2026-10-22')})['session_grid_complete']


def test_intraday_holes_holidays_and_unknown_calendars_are_separate():
    q=requested_grid(task(start='2026-10-20',end='2026-10-22'),{'2026-10-21':full_day(skip=(0,200,374))})
    assert q['expected_sessions']==1 and q['missing_sessions']==0 and q['missing_minutes']==3
    q=requested_grid(task(start='2025-10-20',end='2025-10-21'),{})
    assert not q['calendar_verified'] and not q['session_grid_complete']
    assert requested_grid(task(segment='NSE_CURRENCY'),{})['coverage']=='CALENDAR_OR_INTERVAL_UNVERIFIED'
    assert requested_grid(task(exchange='BSE',segment='BSE_EQ'),{})['coverage']=='CALENDAR_OR_INTERVAL_UNVERIFIED'


def test_daily_grid_checks_dates_not_the_intraday_minute_count():
    rows={'2026-10-21':{'one':{'timestamp':0}}}
    q=requested_grid(task('daily'),rows)
    assert q['missing_sessions']==1 and q['missing_minutes'] is None
    rows['2026-10-22']={'two':{'timestamp':1}}
    assert requested_grid(task('daily'),rows)['session_grid_complete']


def test_fresh_daily_catchup_does_not_reuse_preclose_raw_cache(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);p=Provider();c=Collector(p,s)
    async def call(owner,path,body):return await p.post(path,body)
    monkeypatch.setattr(c,'call',call)
    old=c.create(CollectionPlan(start='2026-10-20',end='2026-10-23',families=['daily'],symbols=['NIFTY'],segments=['IDX_I']))
    c.control(old['id'],'resume');p.answer=candles('2026-10-21')
    result=asyncio.run(c.run(old['id']))
    assert result['counts']['partial']==1 and len(p.calls)==1
    old_entry=c.results(old['id'])['items'][0]
    clock=datetime(2026,10,23,2,tzinfo=timezone.utc)
    slots=schedule_history(c,clock)
    job=c.get_job(slots['core_daily']['job_id'])
    assert job['force_fetch'] and job['state']=='running'
    assert {'INDIA VIX','INDIAVIX'}<=set(job['config']['symbols'])
    p.answer={k:candles('2026-10-21')[k]+candles('2026-10-22')[k] for k in candles()}
    result=asyncio.run(c.run(job['id']))
    assert len(p.calls)==2 and result['counts']['completed']==1
    entry=c.results(job['id'])['items'][0]
    assert entry['result']['session_grid_complete'] and entry['result']['missing_sessions']==0
    assert s.document(old_entry['key'])[0]['result']['missing_sessions']==1
    before=set(s.document_names('jobs/'))
    assert schedule_history(c,clock)['core_daily']['job_id']==job['id']
    assert set(s.document_names('jobs/'))==before
    assert slots['core_rolling']['job_id'] and slots['core_chains']['job_id']


def test_recovery_fetches_partial_request_once_and_retains_the_original(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);p=Provider();c=Collector(p,s)
    async def call(owner,path,body):return await p.post(path,body)
    monkeypatch.setattr(c,'call',call)
    plan=c.create(CollectionPlan(start='2026-10-16',end='2026-10-17',families=['intraday']))
    c.control(plan['id'],'resume');asyncio.run(c.run(plan['id']))
    entry=c.results(plan['id'])['items'][0]
    assert entry['result']['missing_minutes']==374 and eligible(entry)
    s.put_document('control/gap-recovery',{'enabled':True},0)
    tick(c);children=[s.document(k)[0] for k in s.document_names('jobs/') if k!='jobs/'+plan['id']]
    assert len(children)==1 and children[0]['force_fetch']
    tick(c);assert len(s.document_names('jobs/'))==2
    assert s.document(entry['key'])[0]['result']['status']=='partial'
    assert not eligible({'task':entry['task'],'result':{'status':'failed','code':'DH-907','http_status':400}})


def test_manually_paused_core_plan_is_not_resumed_by_schedule(tmp_path):
    s=Store(tmp_path/'s.db');seed(s);c=Collector(Provider(),s)
    clock=datetime(2026,10,23,2,tzinfo=timezone.utc)
    slots=schedule_history(c,clock);jid=slots['core_daily']['job_id']
    c.control(jid,'pause')
    # Even a pre-upgrade manually paused plan must stay paused.
    replace(s,'system/maintenance',lambda old:{**old,'history':{**old['history'],
        'core_daily':{k:v for k,v in old['history']['core_daily'].items() if k!='schedule_version'}}})
    schedule_history(c,clock+timedelta(minutes=2))
    assert c.get_job(jid)['state']=='paused'
    assert s.document('system/maintenance')[0]['history']['core_daily']['job_id']==jid


def test_legacy_cached_response_is_retained_and_replaced_in_a_separate_archive(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);p=Provider();c=Collector(p,s)
    async def call(owner,path,body):return await p.post(path,body)
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start='2026-10-21',end='2026-10-23',families=['daily']))
    c.control(job['id'],'resume')
    original=c.get_job(job['id']);family,item=c.select_item(original);request=c.task(original,family,item)
    key=digest({k:v for k,v in request.items() if k not in ('skip','horizon_gap')})
    stale={'received_at':'2026-10-22T01:00:00+00:00','raw':candles('2026-10-21')}
    s.put_document('responses/'+key,stale,0)
    p.answer={k:candles('2026-10-21')[k]+candles('2026-10-22')[k] for k in candles()}
    result=asyncio.run(c.run(job['id']))
    entry=c.results(job['id'])['items'][0]
    assert result['counts']['completed']==1 and len(p.calls)==1
    assert entry['result']['raw_key']!='responses/'+key
    assert len(s.document(entry['result']['raw_key'])[0]['raw']['timestamp'])==2
    assert s.document('responses/'+key)[0]==stale
    assert not cache_is_closed(request,stale)
    assert cache_is_closed(request,{'received_at':'2026-10-22T20:30:00+00:00'})


def test_release_acceptance_requires_matching_running_build(monkeypatch,tmp_path):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).parents[1]/'deploy/check_worker_release.py'
    spec=importlib.util.spec_from_file_location('release_acceptance',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    expected=module.runtime_build()
    responses=iter([
        {'enabled':True,'worker_active':True,'worker_build_matches':True,'worker_build_id':'old','expected_worker_build_id':expected},
        {'enabled':True,'worker_active':False,'worker_build_matches':True,'worker_build_id':expected,'expected_worker_build_id':expected},
        {'enabled':True,'worker_active':True,'worker_build_matches':True,'worker_build_id':expected,'expected_worker_build_id':expected}])
    sleeps=[]
    assert module.verify(lambda:next(responses),sleeps.append,attempts=3)
    assert sleeps==[5,5]
