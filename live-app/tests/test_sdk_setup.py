import asyncio
import importlib.util
import json
from pathlib import Path
import struct
import time
from types import SimpleNamespace
import pytest
import requests
from fastapi import HTTPException
from provider import Dhan,ProviderError
from sdk_feeds import make_codec,decode_packet
from store import Store
import worker

class Credentials:
    configured=True
    client_id='fixture-client'
    value='fixture-token'
    def token(self):return self.value

COMMON={'securityId':'13','exchangeSegment':'IDX_I','instrument':'INDEX','fromDate':'2026-10-12','toDate':'2026-10-17'}

@pytest.mark.parametrize('path,body',[
    ('charts/historical',COMMON),('charts/intraday',{**COMMON,'interval':'5','oi':True}),
    ('charts/rollingoption',{**COMMON,'exchangeSegment':'NSE_FNO','instrument':'OPTIDX','interval':'1','expiryFlag':'MONTH','expiryCode':1,'strike':'ATM','drvOptionType':'CALL','requiredData':['open','close']}),
    ('optionchain',{'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I','Expiry':'2026-10-20'}),
    ('optionchain/expirylist',{'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I'}),
    ('marketfeed/quote',{'IDX_I':[13]}),('marketfeed/ltp',{'IDX_I':[13]}),('marketfeed/ohlc',{'IDX_I':[13]})])
def test_real_sdk_builds_data_requests_and_preserves_raw_envelope(monkeypatch,path,body):
    calls=[];raw={'data':{'answer':[1,2]},'status':'success'}
    def request(self,method,url,**kw):
        calls.append((url,json.loads(kw['data']),kw))
        r=requests.Response();r.status_code=200;r._content=json.dumps(raw).encode();return r
    monkeypatch.setattr(requests.Session,'request',request)
    before=json.dumps(body,sort_keys=True)
    assert asyncio.run(Dhan(Credentials()).post(path,body))==raw
    url,sent,opts=calls[0]
    assert url=='https://api.dhan.co/v2/'+path
    assert sent['dhanClientId']=='fixture-client' # Official SDK supplies this.
    assert opts['headers']['access-token']=='fixture-token'
    assert opts['allow_redirects'] is False and opts['timeout'][0]==10
    assert json.dumps(body,sort_keys=True)==before # No credential injected into saved provenance.
    if 'interval' in body:assert sent['interval']==int(body['interval'])


def test_token_rotation_uses_fresh_context_and_no_execution_endpoint(monkeypatch):
    tokens=[]
    def request(self,method,url,**kw):
        tokens.append(kw['headers']['access-token'])
        r=requests.Response();r.status_code=200;r._content=b'{"timestamp":[]}';return r
    monkeypatch.setattr(requests.Session,'request',request)
    credentials=Credentials();provider=Dhan(credentials)
    asyncio.run(provider.post('charts/historical',COMMON))
    credentials.value='rotated-token';provider.last=0
    asyncio.run(provider.post('charts/historical',COMMON))
    assert tokens==['fixture-token','rotated-token']
    with pytest.raises(ProviderError)as exc:asyncio.run(provider.post('orders',{}))
    assert exc.value.code=='ENDPOINT' and len(tokens)==2


def test_sdk_handles_http200_provider_failure(monkeypatch):
    def request(self,*a,**kw):
        r=requests.Response();r.status_code=200;r._content=b'{"status":"failure","data":{"code":"807","message":"fixture-token"}}';return r
    monkeypatch.setattr(requests.Session,'request',request)
    with pytest.raises(ProviderError)as exc:asyncio.run(Dhan(Credentials()).post('charts/historical',COMMON))
    assert exc.value.code=='807' and 'fixture-token' not in str(exc.value)


@pytest.mark.parametrize('family',['live','depth20','depth200'])
def test_sdk_feed_subscription_and_real_binary_decoder(family):
    async def run():
        item={'security_id':'13','segment':'NSE_EQ'}
        codec=await asyncio.to_thread(make_codec,Credentials(),family,[item])
        sent=[]
        async def send(packet):sent.append(json.loads(packet))
        codec.ws=SimpleNamespace(send=send)
        await codec.subscribe_instruments()
        if family=='live':packet=struct.pack('<BHBIfI',2,16,1,13,123.5,1791786900)
        else:packet=struct.pack('<hBBiI',28 if family=='depth200' else 332,41,1,13,1)+struct.pack('<dII',123.5,10,2)*(1 if family=='depth200' else 20)
        decoded=decode_packet(codec,packet)
        assert decoded['security_id']==13
        if family=='live':assert decoded['LTP']=='123.50'
        else:assert decoded['depth'][0]['quantity']==10
        assert sent[0]['RequestCode']==(21 if family=='live' else 23)
        assert 'fixture-token' not in json.dumps(sent)
    asyncio.run(run())


def test_worker_runs_existing_checkpoint_until_paused_and_releases_lease(tmp_path,monkeypatch):
    store=Store(tmp_path/'s.db');jid='a'*32
    store.put_document('jobs/'+jid,{'id':jid,'state':'running'},0)
    calls=[]
    class Collector:
        def __init__(self):self.store=store
        async def run(self,jid):
            calls.append(jid)
            worker.replace(store,'jobs/'+jid,lambda j:{**j,'state':'token_required'})
            return {'state':'token_required','items_assessed':0}
    async def sleep(_):pass
    monkeypatch.setattr(worker.asyncio,'sleep',sleep)
    asyncio.run(worker.run_worker(Collector(),max_seconds=10))
    assert calls==[jid]
    assert store.document('jobs/'+jid)[0]['state']=='token_required'
    assert not worker.worker_status(store)['active']


def test_worker_single_owner_and_job_pagination(tmp_path):
    store=Store(tmp_path/'s.db')
    for n in range(105):store.put_document('jobs/'+f'{n:032x}',{'id':f'{n:032x}','state':'running' if n==104 else 'paused'},0)
    assert list(worker.running_jobs(store))==[f'{104:032x}']
    store.put_document('control/worker',{'owner':'other','until':time.time()+100},0)
    asyncio.run(worker.run_worker(SimpleNamespace(store=store),max_seconds=0))
    assert store.document('control/worker')[0]['owner']=='other'


def test_worker_dispatch_failure_retains_checkpoint_and_sanitizes(monkeypatch,tmp_path):
    import google.auth
    store=Store(tmp_path/'s.db')
    monkeypatch.setenv('THETA_COLLECTION_WORKER','projects/theta-pluse/locations/asia-south1/jobs/theta-plus-collector')
    def fail(**kw):raise RuntimeError('do-not-log-secret')
    monkeypatch.setattr(google.auth,'default',fail)
    with pytest.raises(HTTPException)as exc:worker.launch_worker(store)
    assert exc.value.status_code==503 and 'do-not-log' not in exc.value.detail
    assert store.document('control/dispatch')[0] is None


def test_worker_deploy_manifest_uses_existing_identity_and_only_data_config():
    spec=importlib.util.spec_from_file_location('setup_worker',Path(__file__).parents[1]/'deploy/setup_worker.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    revision={'spec':{'serviceAccountName':'existing-sa','containers':[{'image':'uri@sha256:abc','env':[
        {'name':'THETA_GCS_BUCKET','value':'existing-bucket'},{'name':'THETA_DHAN_CREDENTIALS_SECRET','value':'existing-secret-name'},
        {'name':'THETA_TOKEN_ADMIN_HASH','value':'must-not-copy'}]}]}}
    job=m.job_spec(revision)['spec']['template']['spec']['template']['spec']
    assert job['serviceAccountName']=='existing-sa'
    assert job['containers'][0]['args']==['worker.py']
    assert 'must-not-copy' not in json.dumps(job)


def test_cloud_run_retry_reclaims_own_hard_killed_attempt(tmp_path,monkeypatch):
    store=Store(tmp_path/'s.db')
    monkeypatch.setenv('CLOUD_RUN_EXECUTION','execution-1')
    store.put_document('control/worker',{'owner':'killed-attempt','execution':'execution-1','until':time.time()+600},0)
    asyncio.run(worker.run_worker(SimpleNamespace(store=store),max_seconds=10))
    value=store.document('control/worker')[0]
    assert value['owner']!='killed-attempt' and value['state']=='idle' and value['until']==0
