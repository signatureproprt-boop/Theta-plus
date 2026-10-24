import asyncio
from copy import deepcopy
import pytest
from fastapi import HTTPException
from collection import Collector,CollectionPlan,replace
from gap_recovery import tick,rejection_probe_ranges,recovery_progress
from provider import ProviderError
from store import Store
from test_collection import seed,instrument,Provider,candles
from test_session_recovery import full_raw

@pytest.mark.parametrize('family',['daily','intraday'])
@pytest.mark.parametrize('code',['DH-905','DH-907'])
def test_changed_regular_session_recovers_rejection_without_changing_identity(tmp_path,monkeypatch,family,code):
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s);calls=[]
    async def call(owner,path,body):
        calls.append(deepcopy(body))
        if body['fromDate'][:10]=='2026-10-18':raise ProviderError('Rejected',code=code,status=400)
        return full_raw('2026-10-19') if family=='intraday' else candles('2026-10-19')
    monkeypatch.setattr(c,'call',call)
    root=c.create(CollectionPlan(start='2026-10-18',end='2026-10-21',families=[family],continue_rejected_requests=True))
    c.control(root['id'],'resume');asyncio.run(c.run(root['id']))
    entry=c.results(root['id'])['items'][0];original=deepcopy(s.document(entry['key'])[0])
    s.put_document('control/gap-recovery',{'enabled':True},0);tick(c)
    child=next(s.document(k)[0] for k in s.document_names('jobs/') if k!='jobs/'+root['id'])
    assert child['history_slices']==[['2026-10-19','2026-10-20']]
    assert child['config']['families']==[family] and child['force_fetch']
    asyncio.run(c.run(child['id']));done=asyncio.run(c.run(child['id']))
    assert done['counts']['completed']==1 and done['rows_saved']==(375 if family=='intraday' else 1)
    assert calls[0]['securityId']==calls[1]['securityId']=='13'
    assert calls[1]['fromDate'][:10]=='2026-10-19' and calls[1]['toDate'][:10]=='2026-10-20'
    assert s.document(entry['key'])[0]==original
    before=s.document_names('jobs/');replace(s,'control/gap-recovery',lambda q:{**q,'next_scan_at':0});tick(c)
    assert s.document_names('jobs/')==before and len(calls)==2
    report=s.document('control/gap-recovery')[0]['progress']
    assert report['source_results_with_returned_rows']==1 and not report['coverage_verified']


def test_empty_newest_session_probe_stops_before_older_sessions(tmp_path,monkeypatch):
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s);calls=[]
    async def call(owner,path,body):
        calls.append(body)
        if body['fromDate'][:10]=='2026-10-21':raise ProviderError('Rejected',code='DH-907',status=400)
        return {k:[] for k in candles()}
    monkeypatch.setattr(c,'call',call)
    root=c.create(CollectionPlan(start='2026-10-21',end='2026-10-24',families=['intraday'],continue_rejected_requests=True))
    c.control(root['id'],'resume');asyncio.run(c.run(root['id']))
    s.put_document('control/gap-recovery',{'enabled':True},0);tick(c)
    child=next(s.document(k)[0] for k in s.document_names('jobs/') if k!='jobs/'+root['id'])
    assert child['history_slices'][0]==['2026-10-23','2026-10-24'] and len(child['history_slices'])==3
    asyncio.run(c.run(child['id']));done=asyncio.run(c.run(child['id']))
    assert done['state']=='completed' and done['counts']['empty']==1 and done['rows_saved']==0
    assert len(calls)==2 and not done['history_recovery']['probe_verified']


def test_short_probe_requires_changed_bounded_dates_and_verified_exchange_calendar():
    task={'family':'daily','item':instrument(),'variant':{},'from':'2026-10-18','to':'2026-10-21'}
    assert rejection_probe_ranges(task)==[['2026-10-19','2026-10-20']]
    for changes in [{'from':'2026-10-19','to':'2026-10-20'}, {'to':'2026-10-29'},
            {'from':'2025-10-18','to':'2025-10-21'}, {'item':instrument(exchange='BSE',segment='BSE_EQ',instrument='EQUITY')},
            {'item':instrument(reason='Mapping unverified')}, {'from':'2026-10-24','to':'2026-10-25'}]:
        assert rejection_probe_ranges({**task,**changes})==[]


def test_probe_scope_cannot_be_replaced_by_caller_supplied_dates(tmp_path,monkeypatch):
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s)
    async def call(*args):raise ProviderError('Rejected',code='DH-907',status=400)
    monkeypatch.setattr(c,'call',call)
    root=c.create(CollectionPlan(start='2026-10-18',end='2026-10-21',families=['daily'],continue_rejected_requests=True))
    c.control(root['id'],'resume');asyncio.run(c.run(root['id']));entry=c.results(root['id'])['items'][0]
    with pytest.raises(HTTPException):c.retry(root['id'],entry['key'],history_ranges=[['2026-10-23','2026-10-24']])


def test_progress_counts_distinct_sources_and_keeps_partial_outcomes_visible():
    jobs=[{'retry_family':'intraday','retry_of':'a','rows_saved':100,'state':'completed','counts':{'partial':1}},
          {'retry_family':'intraday','retry_of':'a','rows_saved':1,'state':'completed','counts':{'completed':1}},
          {'retry_family':'intraday','retry_of':'b','rows_saved':0,'state':'running','counts':{}}]
    r=recovery_progress(jobs)
    assert r['source_results_assessed']==2 and r['source_results_with_returned_rows']==1
    assert r['retry_jobs']==3 and r['source_results_with_issue_outcomes']==1 and r['source_results_with_pending_jobs']==1
    assert not r['coverage_verified']
    assert recovery_progress([{'retry_of':'x','rows_saved':100,'state':'completed','config':{'families':['quotes']}}])['source_results_with_returned_rows']==0
