import asyncio
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from provider import ProviderError
from store import Store
from session_chart import session_view,refresh_session
import server

def at():return datetime(2026,10,19,3,48,tzinfo=timezone.utc) # 09:18 IST
def raw(offsets=(0,60,120,180)):
    base=int(at().timestamp())-180
    return {'timestamp':[base+x for x in offsets],**{k:[100]*len(offsets) for k in ('open','high','low','close','volume')}}
def record(data):return {'symbol':'nifty','received_at':at().isoformat(),'raw':data}

def test_only_closed_current_session_bars_survive_and_gaps_remain_explicit():
    r=session_view(record(raw((0,120,180))),at())
    assert len(r['candles'])==2 and r['missing_minutes']==1 and r['excluded_rows']==1
    assert r['expected_minutes']==3 and r['freshness']['state']=='FRESH'
    assert r['signal_eligible'] is False

def test_conflicting_duplicate_is_excluded_instead_of_being_silently_chosen():
    data=raw((0,0,60,120));data['close'][1]=101;data['high'][1]=101
    r=session_view(record(data),at())
    assert r['conflicting_minutes']==1 and r['missing_minutes']==1

def test_future_and_off_session_candles_cannot_enter_chart():
    r=session_view(record(raw((-60,0,60,120,180,240))),at())
    assert len(r['candles'])==3 and r['excluded_rows']==3

def test_refresh_is_cached_bounded_and_keeps_collection_documents_unchanged(tmp_path):
    store=Store(tmp_path/'s.db');store.put_document('jobs/keep',{'state':'paused'},0)
    calls=[]
    class Board:
        instruments={'nifty':{'security_id':'13','segment':'IDX_I','instrument':'INDEX'}}
        async def catalog(self):pass
    class Provider:
        async def post(self,path,body):calls.append((path,body));return raw()
    async def run():
        first=await refresh_session(Provider(),store,Board(),'nifty',at())
        second=await refresh_session(Provider(),store,Board(),'nifty',at()+timedelta(seconds=30))
        return first,second
    a,b=asyncio.run(run())
    assert len(calls)==1 and calls[0][0]=='charts/intraday'
    assert calls[0][1]['fromDate']=='2026-10-19 09:15:00'
    assert calls[0][1]['toDate']=='2026-10-19 09:18:00'
    assert a['received_at']==b['received_at']
    assert store.document('jobs/keep')[0]=={'state':'paused'}
    assert not store.rows('dhan_history',10)

def test_invalid_source_does_not_replace_last_valid_snapshot(tmp_path):
    store=Store(tmp_path/'s.db');old={'marker':'retained'};store.put_document('research/session/nifty',old,0)
    class Board:
        instruments={'nifty':{'security_id':'13','segment':'IDX_I','instrument':'INDEX'}}
        async def catalog(self):pass
    class Provider:
        async def post(self,*args):return {'timestamp':[1]}
    with pytest.raises(ProviderError):asyncio.run(refresh_session(Provider(),store,Board(),'nifty',at()))
    assert store.document('research/session/nifty')[0]==old

def test_session_refresh_requires_admin_before_provider_access(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('K_SERVICE','THETA_GCS_BUCKET','THETA_TOKEN_ADMIN_HASH'):monkeypatch.delenv(key,raising=False)
    c=TestClient(server.create_app(tmp_path/'app.db'))
    assert c.post('/api/research/session',json={'symbol':'nifty'}).status_code==503
    assert c.post('/api/research/session',json={'symbol':'other'}).status_code==422
