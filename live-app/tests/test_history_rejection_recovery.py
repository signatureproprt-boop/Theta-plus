import asyncio
from copy import deepcopy

import pytest

from collection import Collector,CollectionPlan,replace
from gap_recovery import eligible,tick
from provider import ProviderError
from store import Store
from test_collection import seed,Provider,candles,instrument


def entry(**changes):
    task={'family':'daily','item':instrument(),'from':'2026-10-21','to':'2026-10-22','variant':{}}
    task.update(changes)
    return {'task':task,'result':{'status':'failed','code':'DH-905','http_status':400}}


def test_identical_valid_history_retries_once_and_retains_failure(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);c=Collector(Provider(),s);requests=[]
    async def call(owner,path,body):
        requests.append((path,deepcopy(body)))
        if len(requests)==1:raise ProviderError('Rejected DH-905',code='DH-905',status=400)
        return candles('2026-10-21')
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start='2026-10-21',end='2026-10-22',families=['daily'],continue_rejected_requests=True))
    c.control(job['id'],'resume');asyncio.run(c.run(job['id']))
    original=c.results(job['id'])['items'][0]
    assert original['result']['status']=='failed' and eligible(original)
    # An earlier scan already passed this failure and is on cooldown. A policy
    # upgrade must revisit it without resetting historical records or totals.
    s.put_document('control/gap-recovery',{'enabled':True,'cursors':{job['id']:original['key']},
        'next_scan_at':10**12,'inspected_total':700,'queued_total':20},0)
    tick(c)
    children=[s.document(k)[0] for k in s.document_names('jobs/') if k!='jobs/'+job['id']]
    assert len(children)==1 and children[0]['force_fetch'] and children[0]['state']=='running'
    assert s.document('control/gap-recovery')[0]['queued_total']==21
    recovered=asyncio.run(c.run(children[0]['id']))
    assert recovered['rows_saved']==1 and recovered['counts']['completed']==1
    assert requests[0]==requests[1]
    replace(s,'control/gap-recovery',lambda old:{**old,'next_scan_at':0})
    tick(c)
    assert len(s.document_names('jobs/'))==2 and len(requests)==2
    assert s.document(original['key'])[0]['result']['status']=='failed'


def test_repeated_provider_rejection_stays_failed_without_a_retry_chain(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db');seed(s);c=Collector(Provider(),s);calls=[]
    async def call(owner,path,body):
        calls.append(body)
        raise ProviderError('Still rejected DH-905',code='DH-905',status=400)
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start='2026-10-21',end='2026-10-22',families=['daily'],continue_rejected_requests=True))
    c.control(job['id'],'resume');asyncio.run(c.run(job['id']))
    s.put_document('control/gap-recovery',{'enabled':True},0);tick(c)
    child=next(s.document(k)[0] for k in s.document_names('jobs/') if k!='jobs/'+job['id'])
    result=asyncio.run(c.run(child['id']))
    assert result['counts']['failed']==1 and result['rows_saved']==0
    replace(s,'control/gap-recovery',lambda old:{**old,'next_scan_at':0});tick(c)
    assert len(s.document_names('jobs/'))==2 and len(calls)==2


@pytest.mark.parametrize('changes',[
    {'family':'rolling'}, {'skip':'outside horizon'},
    {'item':instrument(segment=None)}, {'item':instrument(security_id='bad')},
    {'item':instrument(reason='ambiguous master')},
    {'item':instrument(instrument='INDEX',segment='NSE_EQ')},
    {'from':'not-a-date'}, {'to':'2026-10-20'},
    {'variant':{'securityId':'different'}},
    {'family':'intraday','variant':{'interval':'2'}},
    {'family':'intraday','variant':{'interval':'1'},'from':'2020-01-01'},
])
def test_invalid_or_unsupported_requests_are_not_automatically_replayed(changes):
    assert not eligible(entry(**changes))


@pytest.mark.parametrize('code',['DH-907','DH-901','DH-902','805','806'])
def test_no_data_auth_and_rate_errors_are_not_misclassified(code):
    e=entry();e['result']['code']=code
    assert not eligible(e)


def test_supported_intraday_request_is_eligible_for_one_retry():
    assert eligible(entry(family='intraday',variant={'interval':'1'}))
