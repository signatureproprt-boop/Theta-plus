import asyncio
from datetime import datetime,timedelta,timezone
import pytest
from collection import Collector,CollectionPlan,replace
from provider import ProviderError
from runtime import IST
from store import Store
from test_collection import seed,Provider,candles
from worker import resume_legacy_budget_pause


def test_daily_guard_waits_without_advancing_and_resumes_same_task(tmp_path,monkeypatch):
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s);calls=[]
    async def call(owner,path,body):
        calls.append(dict(body))
        if len(calls)==1:raise ProviderError('Daily API budget reached',code='BUDGET',status=429)
        return candles('2026-10-21')
    monkeypatch.setattr(c,'call',call)
    job=c.create(CollectionPlan(start='2026-10-21',end='2026-10-22',families=['daily']))
    c.control(job['id'],'resume');waiting=asyncio.run(c.run(job['id']))
    assert waiting['state']=='running' and waiting['items_assessed']==0 and waiting['provider_calls']==0
    retry=datetime.fromtimestamp(waiting['retry_at'],IST)
    assert retry.date()==datetime.now(IST).date()+timedelta(days=1) and (retry.hour,retry.minute,retry.second)==(0,0,5)
    owner=c.acquire
    def forbidden():raise AssertionError('A waiting job must not acquire the collector lease')
    monkeypatch.setattr(c,'acquire',forbidden);asyncio.run(c.run(job['id']));assert len(calls)==1
    monkeypatch.setattr(c,'acquire',owner)
    replace(s,'jobs/'+job['id'],lambda old:{**old,'retry_at':0})
    recovered=asyncio.run(c.run(job['id']))
    assert recovered['rows_saved']==1 and recovered['counts']['completed']==1 and calls[0]==calls[1]
    assert 'budget_wait_day' not in recovered


@pytest.mark.parametrize('message',['Daily API budget reached','Daily market-data request budget reached'])
def test_legacy_quota_pause_resumes_on_next_ist_day_only(tmp_path,message):
    s=Store(tmp_path/'db');jid='a'*32
    job={'id':jid,'state':'paused','message':message,'updated_at':'2026-10-23T17:00:00+00:00','current':{'exact':'task'},'cursor':{'window':7}}
    s.put_document('jobs/'+jid,job,0)
    same=resume_legacy_budget_pause(s,job,datetime(2026,10,23,17,1,tzinfo=timezone.utc));assert same['state']=='paused'
    resumed=resume_legacy_budget_pause(s,job,datetime(2026,10,23,19,tzinfo=timezone.utc))
    assert resumed['state']=='running' and resumed['current']==job['current'] and resumed['cursor']==job['cursor']


@pytest.mark.parametrize('change',[{'system_user_paused':True},{'message':'Manually paused'},{'message':'Permission required'},{'updated_at':'bad'},{'updated_at':'2026-10-22T17:00:00'}])
def test_manual_and_unrelated_pauses_are_never_auto_resumed(tmp_path,change):
    s=Store(tmp_path/'db');job={'id':'a'*32,'state':'paused','message':'Daily API budget reached','updated_at':'2026-10-22T17:00:00+00:00',**change}
    s.put_document('jobs/'+job['id'],job,0)
    assert resume_legacy_budget_pause(s,job,datetime(2026,10,24,tzinfo=timezone.utc))==job
