import asyncio
from copy import deepcopy
from datetime import date,datetime,timedelta
import pytest
from collection import Collector,CollectionPlan,replace
from gap_recovery import daily_probe_ranges,eligible,tick
from provider import ProviderError
from store import Store
from test_collection import seed,Provider,candles,instrument


def setup(tmp_path,monkeypatch,probe_rows=True):
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s);calls=[]
    original=('2026-01-01','2026-10-23')
    async def call(owner,path,body):
        span=(body['fromDate'][:10],body['toDate'][:10]);calls.append(span)
        if span==original:raise ProviderError('Long range rejected',code='DH-907',status=400)
        if not probe_rows:return {k:[] for k in candles()}
        at=date.fromisoformat(span[0])+timedelta(days=1)
        return candles(at.isoformat())
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start=original[0],end=original[1],families=['daily'],continue_rejected_requests=True))
    c.control(job['id'],'resume');asyncio.run(c.run(job['id']))
    entry=c.results(job['id'])['items'][0]
    s.put_document('control/gap-recovery',{'enabled':True},0);tick(c)
    child=next(s.document(k)[0] for k in s.document_names('jobs/') if k!='jobs/'+job['id'])
    return s,c,calls,entry,child


def test_positive_bounded_probe_recovers_earlier_ranges_without_date_holes(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch)
    expected=daily_probe_ranges(entry['task'])
    assert expected[0]==['2026-09-23','2026-10-23']
    for _ in range(len(expected)+1):asyncio.run(c.run(child['id']))
    done=c.get_job(child['id'])
    assert done['state']=='completed' and done['slice_probe_verified']
    assert done['rows_saved']==len(expected) and calls[1:]==[tuple(r) for r in expected]
    ordered=sorted(expected);assert ordered[0][0]==entry['task']['from'] and ordered[-1][1]==entry['task']['to']
    assert all(a[1]==b[0] for a,b in zip(ordered,ordered[1:]))
    assert s.document(entry['key'])[0]['result']['status']=='failed'
    result=c.summary(done)
    assert 'history_slices' not in result and result['history_recovery']['windows_assessed']==len(expected)
    assert result['history_recovery']['probe_verified']
    before=s.document_names('jobs/');replace(s,'control/gap-recovery',lambda old:{**old,'next_scan_at':0});tick(c)
    assert s.document_names('jobs/')==before


def test_empty_probe_stops_without_inferring_unavailable_history(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch,probe_rows=False)
    asyncio.run(c.run(child['id']));done=asyncio.run(c.run(child['id']))
    assert done['state']=='completed' and done['rows_saved']==0 and len(calls)==2
    assert not done['history_recovery']['probe_verified']
    assert 'remains unresolved' in done['message']
    assert c.results(child['id'])['items'][0]['result']['status']=='empty'


def test_rejected_probe_does_not_enter_earlier_windows(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch)
    async def reject(*args):raise ProviderError('Still rejected',code='DH-907',status=400)
    monkeypatch.setattr(c,'call',reject)
    asyncio.run(c.run(child['id']));done=asyncio.run(c.run(child['id']))
    assert done['state']=='completed' and done['counts']['failed']==1 and done['rows_saved']==0
    assert len(c.results(child['id'])['items'])==1


def test_quarantined_invalid_probe_does_not_unlock_old_ranges(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch)
    async def invalid(*args):return candles('2026-10-21',{'high':[5]})
    monkeypatch.setattr(c,'call',invalid)
    asyncio.run(c.run(child['id']));done=asyncio.run(c.run(child['id']))
    assert done['rows_saved']==0 and done['state']=='completed'
    assert not done['history_recovery']['probe_verified']


def test_old_identical_rejection_can_be_followed_by_one_bounded_probe(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch)
    # An unrelated previous unchanged attempt is preserved; it does not suppress
    # a distinct date-window strategy. Conversely a second split job is blocked.
    previous=c.retry(entry['key'].split('/')[1],entry['key'])
    replace(s,'control/gap-recovery',lambda old:{**old,'next_scan_at':0,'position':0,'cursors':{}})
    before=s.document_names('jobs/');tick(c)
    assert s.document_names('jobs/')==before and c.get_job(previous['id'])['state']=='paused'


def test_date_windows_are_bounded_even_for_a_century_of_daily_history():
    t={'family':'daily','item':instrument(),'variant':{},'from':'1900-01-01','to':'2026-10-23'}
    ranges=daily_probe_ranges(t)
    assert 1<len(ranges)<=128
    assert sorted(ranges)[0][0]==t['from'] and sorted(ranges)[-1][1]==t['to']
    t['family']='intraday';assert daily_probe_ranges(t)==[]
    t.update(family='daily',item=instrument(segment=None,reason='unmapped'));assert daily_probe_ranges(t)==[]


def test_bounded_probe_admission_respects_recovery_capacity(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch)
    for n in range(7):
        clone=deepcopy(child);clone['id']=f'{n+100:032x}';s.put_document('jobs/'+clone['id'],clone,0)
    replace(s,'control/gap-recovery',lambda old:{**old,'next_scan_at':0})
    before=s.document('control/gap-recovery')[0]
    assert not tick(c)
    assert s.document('control/gap-recovery')[0]==before and len(s.document_names('jobs/'))==9


def test_one_remaining_slot_admits_only_one_window_job_and_retains_scan_checkpoint(tmp_path,monkeypatch):
    s,c,calls,entry,child=setup(tmp_path,monkeypatch)
    for n in range(6):
        clone=deepcopy(child);clone['id']=f'{n+100:032x}';s.put_document('jobs/'+clone['id'],clone,0)
    jid=entry['key'].split('/')[1]
    for n in range(1,6):
        extra=deepcopy(entry);extra.pop('key');extra['task']['item']['security_id']=str(1000+n)
        s.put_document(f'results/{jid}/{n:064x}',extra,0)
    replace(s,'control/gap-recovery',lambda old:{**old,'next_scan_at':0,'position':0,'cursors':{}})
    assert tick(c)
    jobs=[s.document(k)[0] for k in s.document_names('jobs/')]
    assert sum(bool(j.get('history_slices')) and j['state']=='running' for j in jobs)==8
    assert s.document('control/gap-recovery')[0]['cursors'][jid]==f'results/{jid}/{1:064x}'
