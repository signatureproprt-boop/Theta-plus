import asyncio
import json
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
import requests

from collection import Collector,SEGMENTS,replace
from provider import Dhan,ProviderError
from segment_probe import check,public_report,REPORT
from store import Store
from test_sdk_setup import Credentials


def setup(tmp_path,provider):
    store=Store(tmp_path/'s.db');collector=Collector(provider,store)
    expiry=(datetime.now(timezone.utc).date()+timedelta(days=20)).isoformat()
    entries=[{'key':'future','exchange':'NSE','raw_segment':'M','segment':None,'security_id':'101',
        'instrument':'FUTCOM','symbol':'GOLD','expiry':expiry,'reason':'No documented API segment mapping: NSE_M'},
        {'key':'option','exchange':'NSE','raw_segment':'M','segment':None,'security_id':'102',
        'instrument':'OPTFUT','symbol':'GOLD','expiry':expiry,'reason':'No documented API segment mapping: NSE_M'}]
    pages=[]
    for entry in entries:
        key='catalogue/'+'a'*64+'/pages/'+entry['key']
        store.put_document(key,{'items':[entry]},0)
        pages.append({'key':key,'group':'UNMAPPED-'+entry['instrument']})
    cat={'sha256':'a'*64,'rows':2,'pages':pages,'fetched_at':datetime.now(timezone.utc).isoformat()}
    store.put_document('catalogue/current',cat,0)
    return collector,cat


def test_default_sdk_still_rejects_candidate_but_bounded_probe_uses_shared_budget(monkeypatch,tmp_path):
    sent=[]
    def request(self,method,url,**kw):
        sent.append((url,json.loads(kw['data'])))
        response=requests.Response();response.status_code=200
        response._content=b'{"status":"success","data":{"NSE_COMM":{"101":{"last_price":100}}}}'
        return response
    monkeypatch.setattr(requests.Session,'request',request)
    provider=Dhan(Credentials(),Store(tmp_path/'s.db'))
    with pytest.raises(ProviderError):asyncio.run(provider.post('marketfeed/quote',{'NSE_COMM':[101]}))
    assert sent==[]
    provider.last=0
    asyncio.run(provider.probe_commodity_quotes([101,102]))
    assert sent==[('https://api.dhan.co/v2/marketfeed/quote',{'NSE_COMM':[101,102]})]
    assert provider.store.document('control/provider-budget')[0]['calls']==2
    with pytest.raises(ProviderError):asyncio.run(provider.probe_commodity_quotes([101,102,103]))
    assert len(sent)==1 and ('NSE','M') not in SEGMENTS


def test_provider_rejection_is_retained_without_changing_any_catalogue_mapping(tmp_path):
    class Provider:
        calls=[]
        async def probe_commodity_quotes(self,ids):
            self.calls.append(ids)
            raise ProviderError('Dhan rejected candidate',code='814',status=400)
    p=Provider();c,cat=setup(tmp_path,p);before=deepcopy(c.catalogue())
    report=asyncio.run(check(c))
    assert report['state']=='PROVIDER_REJECTED' and not report['mapping_changed']
    assert report['probes'][0]['code']=='814'
    assert c.catalogue()==before and c.store.document('control/lease')[0]['until']==0
    assert len(c.store.rows('segment_probe_source'))==1
    assert asyncio.run(check(c))['cached'] and p.calls==[[101,102]]


def test_returned_quote_and_daily_rows_are_separate_evidence_and_raw_is_private(tmp_path,monkeypatch):
    class Provider:
        async def probe_commodity_quotes(self,ids):
            return {'data':{'NSE_COMM':{str(i):{'last_price':100} for i in ids}}}
    c,cat=setup(tmp_path,Provider());requests_seen=[]
    async def call(owner,path,body):
        assert c.store.document('control/lease')[0]['owner']==owner
        requests_seen.append((path,body))
        return {key:[60 if key=='timestamp' else 1] for key in ('open','high','low','close','volume','timestamp')}
    monkeypatch.setattr(c,'call',call)
    report=asyncio.run(check(c))
    assert report['state']=='QUOTE_RETURNED' and not report['mapping_changed']
    assert report['probes'][1]['rows']==1 and requests_seen[0][1]['securityId']=='101'
    assert requests_seen[0][1]['exchangeSegment']=='NSE_COMM'
    assert c.catalogue()==cat and 'raw' not in report and 'raw' not in public_report(c.store)
    assert c.store.document(REPORT)[0]['raw']['quote']


@pytest.mark.parametrize('value',[{}, {'error':'invalid id'}, {'last_price':0},{'last_price':True},{'last_price':float('nan')}])
def test_empty_or_invalid_observations_never_establish_candidate_support(tmp_path,value):
    class Provider:
        async def probe_commodity_quotes(self,ids):return {'data':{'NSE_COMM':{'101':value}}}
    c,_=setup(tmp_path,Provider())
    # Keep the NaN test at the response boundary; the retained source rejects
    # non-JSON numbers, as required by the shared durable store contract.
    if value.get('last_price')!=value.get('last_price'):
        with pytest.raises(ValueError):asyncio.run(check(c))
        assert public_report(c.store)['state']=='NOT_CHECKED'
    else:
        r=asyncio.run(check(c));assert r['state']=='NO_MATCHING_QUOTE' and len(r['probes'])==1


def test_stale_master_does_not_send_any_probe(tmp_path):
    c,cat=setup(tmp_path,SimpleNamespace())
    cat['fetched_at']='2000-01-01T00:00:00+00:00'
    c.store.put_document('catalogue/current',cat,c.store.document('catalogue/current')[1])
    assert asyncio.run(check(c))['state']=='CATALOGUE_STALE'
    assert c.store.document('control/lease')[0] is None


def test_unattended_probe_refreshes_on_new_ist_day_and_uses_busy_cooldown(tmp_path,monkeypatch):
    import segment_probe as module
    from datetime import datetime,timezone,timedelta
    class Provider:pass
    c,cat=setup(tmp_path,Provider());cat['review_reasons']={'NSE_M':2}
    replace(c.store,'catalogue/current',lambda _:cat)
    clock=datetime(2026,10,24,4,tzinfo=timezone.utc);calls=[]
    async def busy(collector):calls.append(1);return {'state':'COLLECTOR_BUSY'}
    monkeypatch.setattr(module,'check',busy)
    asyncio.run(module.refresh_if_due(c,clock));asyncio.run(module.refresh_if_due(c,clock+timedelta(minutes=20)))
    assert len(calls)==1
    asyncio.run(module.refresh_if_due(c,clock+timedelta(hours=1)));assert len(calls)==2
    replace(c.store,REPORT,lambda _:{'checked_at':clock.isoformat(),'catalogue_sha256':cat['sha256'],'state':'NO_MATCHING_QUOTE'})
    asyncio.run(module.refresh_if_due(c,clock+timedelta(hours=2)));assert len(calls)==2
    asyncio.run(module.refresh_if_due(c,clock+timedelta(days=1)));assert len(calls)==3
