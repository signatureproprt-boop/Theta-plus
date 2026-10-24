import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest
from fastapi import HTTPException
from collection import Collector,CollectionPlan,replace,digest
from session_recovery import tick,session_days,saved_session,REPORT
from store import Store
from test_collection import seed,Provider,candles

CLOCK=datetime(2026,10,24,6,tzinfo=timezone.utc)

def full_raw(day):
    row=candles(day);first=row['timestamp'][0]-60
    return {k:([first+n*60 for n in range(375)] if k=='timestamp' else [v[0]]*375) for k,v in row.items()}

def setup(tmp_path,monkeypatch,family='intraday'):
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s);calls=[]
    a=full_raw('2026-10-21') if family=='intraday' else candles('2026-10-21')
    b=full_raw('2026-10-22') if family=='intraday' else candles('2026-10-22')
    answer={k:a[k]+b[k] for k in a}
    async def call(owner,path,body):calls.append(deepcopy(body));return deepcopy(answer)
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start='2026-10-21',end='2026-10-24',families=[family]))
    c.control(job['id'],'resume');asyncio.run(c.run(job['id']));asyncio.run(c.run(job['id']))
    entry=c.results(job['id'])['items'][0]
    s.put_document('system/maintenance',{'history':{'core_'+family:{'job_id':job['id']}}},0)
    return s,c,job,entry,calls,answer

@pytest.mark.parametrize('family',['daily','intraday'])
def test_exhausted_generic_retry_does_not_block_exact_missing_session(tmp_path,monkeypatch,family):
    s,c,root,entry,calls,answer=setup(tmp_path,monkeypatch,family)
    generic=c.retry(root['id'],entry['key']);c.control(generic['id'],'resume')
    asyncio.run(c.run(generic['id']));asyncio.run(c.run(generic['id']))
    original=deepcopy(s.document(entry['key'])[0]);report=tick(c,CLOCK)
    assert report['queued_this_scan']==1 and report['missing_sessions']==1
    child=report['sessions'][-1]['job_id'];j=c.get_job(child)
    assert j['current']['from']=='2026-10-23' and j['current']['to']=='2026-10-24' and j['force_fetch']
    assert j['system_role'].startswith('core_')
    assert tick(c,CLOCK)['queued_this_scan']==0
    answer.clear();answer.update(full_raw('2026-10-23') if family=='intraday' else candles('2026-10-23'))
    asyncio.run(c.run(child));asyncio.run(c.run(child))
    assert calls[-1]['fromDate'].startswith('2026-10-23') and calls[-1]['toDate'].startswith('2026-10-24')
    done=tick(c,CLOCK+timedelta(minutes=1))
    assert done['saved_sessions_complete']==(3 if family=='intraday' else 1) and done['missing_sessions']==0 and not done['coverage_verified']
    assert done['sessions'][-1]['rows']==(375 if family=='intraday' else 1)
    assert s.document(entry['key'])[0]==original
    assert c.get_job(generic['id'])['counts']['partial']==1


def test_no_data_retries_are_delayed_bounded_and_never_marked_complete(tmp_path,monkeypatch):
    s,c,root,entry,calls,answer=setup(tmp_path,monkeypatch)
    answer.clear();answer.update({k:[] for k in candles()})
    first=tick(c,CLOCK)['sessions'][-1]['job_id']
    asyncio.run(c.run(first));asyncio.run(c.run(first))
    replace(s,'jobs/'+first,lambda j:{**j,'updated_at':CLOCK.isoformat()})
    assert tick(c,CLOCK+timedelta(hours=1))['queued_this_scan']==0
    second=tick(c,CLOCK+timedelta(hours=2))['sessions'][-1]['job_id'];assert second!=first
    asyncio.run(c.run(second));asyncio.run(c.run(second))
    final=tick(c,CLOCK+timedelta(days=1))
    assert final['sessions'][-1]['state']=='UNRESOLVED_AFTER_BOUNDED_RETRIES'
    assert final['missing_sessions']==1 and not final['sessions'][-1]['session_grid_complete']
    before=s.document_names('jobs/');tick(c,CLOCK+timedelta(days=2));assert s.document_names('jobs/')==before


def test_wrong_identity_and_invalid_rows_cannot_certify_saved_session(tmp_path,monkeypatch):
    s,c,root,entry,calls,answer=setup(tmp_path,monkeypatch)
    task={**entry['task'],'from':'2026-10-23','to':'2026-10-24'}
    result=c.persist_candles(task,{'raw':full_raw('2026-10-23')});key='candles/'+result['series']+'/2026-10-23'
    assert saved_session(c,task,'2026-10-23')['session_grid_complete']
    original=deepcopy(s.document(key)[0])
    replace(s,key,lambda d:{**d,'instrument':{**d['instrument'],'security_id':'25'}})
    assert not saved_session(c,task,'2026-10-23')['session_grid_complete']
    replace(s,key,lambda _:deepcopy(original))
    def corrupt(d):
        k=next(iter(d['candles']));d['candles'][k]['high']=0;return d
    replace(s,key,corrupt)
    assert not saved_session(c,task,'2026-10-23')['session_grid_complete']


def test_invalid_or_foreign_session_cannot_expand_request_scope(tmp_path,monkeypatch):
    s,c,root,entry,_,_=setup(tmp_path,monkeypatch)
    for day in ('2026-10-20','2026-10-24','2026-10-25','bad'):
        with pytest.raises(HTTPException):c.retry(root['id'],entry['key'],session_day=day)
    e=deepcopy(entry);e['task']['item']['segment']='NSE_EQ';assert session_days(e)==[]
    e=deepcopy(entry);e['task']['from']='2026-01-01';assert session_days(e)==[]
    replace(s,'jobs/'+root['id'],lambda j:{**j,'system_user_paused':True})
    assert tick(c,CLOCK)['queued_this_scan']==0


def test_current_saved_rows_choose_only_the_remaining_incomplete_session(tmp_path,monkeypatch):
    s,c,root,entry,_,_=setup(tmp_path,monkeypatch)
    task={**entry['task'],'from':'2026-10-23','to':'2026-10-24'}
    complete=c.persist_candles(task,{'raw':full_raw('2026-10-23')})
    # Another retry has already filled the originally absent date. A different
    # day now has one missing minute; scan retained candles, not the old counts.
    key='candles/'+complete['series']+'/2026-10-22'
    def remove_minute(doc):doc['candles'].pop(next(iter(doc['candles'])));return doc
    replace(s,key,remove_minute)
    report=tick(c,CLOCK)
    assert report['queued_this_scan']==1
    missing=[i for i in report['sessions'] if not i['session_grid_complete']]
    assert len(missing)==1 and missing[0]['day']=='2026-10-22' and missing[0]['missing_minutes']==1
    assert c.get_job(missing[0]['job_id'])['current']['from']=='2026-10-22'
