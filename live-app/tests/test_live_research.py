import asyncio
from datetime import datetime,timezone,timedelta
import json
import struct
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from dhanhq.marketfeed import MarketFeed
import live_research as live
from store import Store
import server

def at():return datetime(2026,10,19,8,0,tzinfo=timezone.utc)

def codec():return object.__new__(MarketFeed)

def ticker(sid=13,epoch=None,price=100):
    return struct.pack('<BHBIfI',2,16,0,sid,price,int((epoch or at()).timestamp())+19800)

def instruments():return {(0,'13'):{'segment':'IDX_I','security_id':'13','key':'nifty','role':'underlying','label':'NIFTY'}}

def test_epoch_keeps_date_not_just_sdk_time_and_never_implies_quote_time():
    row=live.observation(codec(),ticker(),instruments(),at()+timedelta(seconds=1))
    assert row['trade_at']=='2026-10-19T08:00:00+00:00'
    assert row['raw_ltt_epoch']==int(at().timestamp())+19800
    assert row['trade_state']=='RECENT_TRADE' and row['ltp']==100
    assert row['quote_at'] is None and not row['signal_eligible']

def test_live_ist_epoch_normalization_preserves_utc_date_at_midnight_boundary():
    traded=datetime(2026,10,18,20,0,tzinfo=timezone.utc)
    row=live.observation(codec(),ticker(epoch=traded),instruments(),traded)
    assert row['trade_at']=='2026-10-18T20:00:00+00:00'
    assert row['trade_state']=='RECENT_TRADE'


@pytest.mark.parametrize('delta,expected',[(11,'STALE_TRADE'),(-1,'FUTURE_TIMESTAMP')])
def test_old_and_future_trade_time_fail_closed(delta,expected):
    row=live.observation(codec(),ticker(),instruments(),at()+timedelta(seconds=delta))
    assert row['trade_state']==expected

def test_split_coalesced_packets_and_reject_truncation():
    assert len(list(live.split_packets(ticker()+ticker())))==2
    with pytest.raises(ValueError):list(live.split_packets(ticker()[:-1]))
    with pytest.raises(ValueError):list(live.split_packets('token-bearing-text'))

def test_unknown_security_and_previous_close_do_not_become_ticks():
    assert live.observation(codec(),ticker(sid=999),instruments(),at()) is None
    assert live.observation(codec(),bytes([6])+b'0'*15,instruments(),at()) is None

def test_full_quote_spread_and_oi_with_no_fabricated_quote_timestamp():
    depth=struct.pack('<IIHHff',100,200,1,2,99,101)*5
    packet=struct.pack('<BHBIfHIfIIIIIIffff100s',8,162,2,123,100,10,int(at().timestamp())+19800,100,1000,100,200,500,501,499,100,99,101,98,depth)
    lookup={(2,'123'):{'segment':'NSE_FNO','security_id':'123','role':'option'}}
    row=live.observation(codec(),packet,lookup,at())
    assert row['spread_pct']==2 and row['oi']==500 and row['volume']==1000
    assert row['quote_state']=='EXCHANGE_QUOTE_TIME_UNAVAILABLE'

def test_lease_separate_from_collection_and_wrong_owner_cannot_release(tmp_path):
    s=Store(tmp_path/'s.db');s.put_document('control/lease',{'owner':'collector'},0)
    owner=live.claim(s)
    with pytest.raises(HTTPException) as exc:live.claim(s)
    assert exc.value.status_code==409
    live.release(s,'wrong');assert s.document('research/live-lease')[0]['owner']==owner
    live.release(s,owner);assert s.document('research/live-lease')[0] is None
    assert s.document('control/lease')[0]=={'owner':'collector'}

def test_disconnect_returns_safe_provider_code_only():
    packet=struct.pack('<BHBIH',50,10,0,13,805)
    assert live.observation(codec(),packet,instruments(),at())=={'type':'status','state':'PROVIDER_DISCONNECT','code':805}

def test_stream_failure_sanitizes_errors_and_releases_lease(tmp_path,monkeypatch):
    s=Store(tmp_path/'s.db')
    async def prepare(*args):raise RuntimeError('wss://bad?token=never-expose')
    monkeypatch.setattr(live,'prepare',prepare)
    async def run():return [v async for v in live.stream(None,s,None,'nifty',None)]
    values=asyncio.run(run());joined=''.join(values)
    assert 'UNAVAILABLE' in joined and 'never-expose' not in joined
    assert s.document('research/live-lease')[0] is None

def test_stream_subscribes_emits_tick_and_cleans_up_provider_disconnect(tmp_path,monkeypatch):
    s=Store(tmp_path/'stream.db');sent=[]
    class Socket:
        def __init__(self):self.frames=[ticker(),struct.pack('<BHBIH',50,10,0,13,805)]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def recv(self):return self.frames.pop(0)
        async def send(self,value):sent.append(json.loads(value))
    class Codec:
        async def subscribe_instruments(self):await self.ws.send('{"RequestCode":21}')
        def process_data(self,packet):return codec().process_data(packet)
    class Credentials:
        client_id='fixture-client'
        def token(self):return 'fixture-token'
    class Provider:credentials=Credentials()
    class Request:
        async def is_disconnected(self):return False
    async def prepare(*args):return list(instruments().values())
    monkeypatch.setattr(live,'prepare',prepare)
    monkeypatch.setattr(live,'make_codec',lambda *args:Codec())
    monkeypatch.setattr(live.websockets,'connect',lambda *args,**kwargs:Socket())
    monkeypatch.setattr(live,'now',at)
    async def run():return [json.loads(v.removeprefix('data: ')) async for v in live.stream(Provider(),s,None,'nifty',Request())]
    values=asyncio.run(run())
    assert sent==[{'RequestCode':21}]
    assert any(v.get('state')=='CONNECTED_AWAITING_TICKS' for v in values)
    tick=next(v['ticks'][0] for v in values if v.get('ticks'))
    assert tick['trade_state']=='RECENT_TRADE' and tick['key']=='nifty'
    assert values[-1]['state']=='PROVIDER_DISCONNECT'
    assert 'fixture-token' not in json.dumps(values)
    assert s.document('research/live-lease')[0] is None

def test_live_route_requires_existing_admin_and_rejects_unknown_symbols(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('K_SERVICE','THETA_GCS_BUCKET','THETA_TOKEN_ADMIN_HASH'):monkeypatch.delenv(key,raising=False)
    c=TestClient(server.create_app(tmp_path/'app.db'))
    assert c.post('/api/research/live',json={'symbol':'nifty'}).status_code==503
    assert c.post('/api/research/live',json={'symbol':'arbitrary-security'}).status_code==422

def test_asgi_disconnect_cancellation_releases_owned_lease(tmp_path,monkeypatch):
    import anyio
    s=Store(tmp_path/'cancel.db')
    async def run():
        started=anyio.Event()
        async def prepare(*args):
            started.set()
            await anyio.sleep_forever()
        monkeypatch.setattr(live,'prepare',prepare)
        async def consume():
            async for _ in live.stream(None,s,None,'nifty',None):pass
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await started.wait()
            assert s.document('research/live-lease')[0]
            group.cancel_scope.cancel()
        assert s.document('research/live-lease')[0] is None
    anyio.run(run)


def test_cancel_during_claim_does_not_orphan_owner(tmp_path,monkeypatch):
    import anyio
    import threading
    s=Store(tmp_path/'claim-cancel.db');claimed=threading.Event();finish=threading.Event()
    original=live.claim
    def slow_claim(store):
        owner=original(store);claimed.set();finish.wait(timeout=3);return owner
    monkeypatch.setattr(live,'claim',slow_claim)
    async def prepare(*args):await anyio.sleep_forever()
    monkeypatch.setattr(live,'prepare',prepare)
    async def run():
        async def consume():
            async for _ in live.stream(None,s,None,'nifty',None):pass
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await anyio.to_thread.run_sync(claimed.wait)
            group.cancel_scope.cancel();finish.set()
        assert s.document('research/live-lease')[0] is None
    anyio.run(run)


def test_busy_stream_is_retryable_without_stealing_owner(tmp_path):
    s=Store(tmp_path/'busy.db');owner=live.claim(s)
    async def run():return [json.loads(v.removeprefix('data: ')) async for v in live.stream(None,s,None,'nifty',None)]
    values=asyncio.run(run())
    assert values[0]['state']=='STREAM_BUSY' and values[0]['retry_after_seconds']==5
    assert s.document('research/live-lease')[0]['owner']==owner


def test_prepare_subscribes_displayed_window_and_pinned_exact_contract(tmp_path,monkeypatch):
    from types import SimpleNamespace
    s=Store(tmp_path/'window.db');expiry='2026-10-27'
    pairs={};options={}
    for i in range(21):
        strike=22000+i*50;pairs[str(strike)]={}
        for side,leg in [('CE','ce'),('PE','pe')]:
            sid=str(strike*2+(side=='PE'))
            pairs[str(strike)][leg]={'security_id':sid}
            options[sid]={'security_id':sid,'expiry':expiry,'strike':strike,'side':side,'lot_size':65,'underlying':'nifty'}
    record=s.add('dhan_chain',{'raw':{'data':{'oc':pairs,'last_price':22500}}})
    async def catalog():pass
    async def fetch(*args):return {'id':record,'expiry':expiry}
    monkeypatch.setattr(live,'fetch_chain',fetch);monkeypatch.setattr(live,'now',at)
    board=SimpleNamespace(catalog=catalog,instruments={'nifty':{'security_id':'13','segment':'IDX_I'}},options=options)
    pinned=str(22000*2)
    result=asyncio.run(live.prepare(board,None,s,'nifty',pinned))
    legs=[r for r in result if r['role']=='option']
    assert len(legs)==23  # displayed ATM +/-5 CE/PE plus pinned outside window
    assert sum(r['security_id']==pinned for r in legs)==1
    assert {r['strike'] for r in legs if r['security_id']!=pinned}==set(range(22250,22800,50))
    # A valid ID on another underlying cannot be attached to this room.
    options['999']={**options[pinned],'security_id':'999','underlying':'banknifty'}
    assert not any(r['security_id']=='999' for r in asyncio.run(live.prepare(board,None,s,'nifty','999')))
    options['999'].update(underlying='nifty',expiry='2026-10-15')
    assert not any(r['security_id']=='999' for r in asyncio.run(live.prepare(board,None,s,'nifty','999')))

@pytest.mark.parametrize('count',[3,300])
def test_repeated_same_instrument_observations_reach_browser_in_order(tmp_path,monkeypatch,count):
    s=Store(tmp_path/'ordered.db')
    class Socket:
        def __init__(self):self.frames=[b''.join(ticker(price=100+i) for i in range(count)),struct.pack('<BHBIH',50,10,0,13,805)]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def recv(self):return self.frames.pop(0)
    class Codec:
        async def subscribe_instruments(self):pass
        def process_data(self,packet):return codec().process_data(packet)
    class Credentials:
        client_id='fixture'
        def token(self):return 'fixture'
    class Provider:credentials=Credentials()
    class Request:
        async def is_disconnected(self):return False
    async def prepare(*args):return list(instruments().values())
    monkeypatch.setattr(live,'prepare',prepare);monkeypatch.setattr(live,'make_codec',lambda *args:Codec())
    monkeypatch.setattr(live.websockets,'connect',lambda *args,**kwargs:Socket());monkeypatch.setattr(live,'now',at)
    async def run():return [json.loads(v.removeprefix('data: ')) async for v in live.stream(Provider(),s,None,'nifty',Request())]
    values=asyncio.run(run());batches=[v for v in values if v.get('type')=='batch'];ticks=[t for b in batches for t in b['ticks']]
    assert [t['ltp'] for t in ticks]==list(range(100,100+count))
    assert [t['stream_sequence'] for t in ticks]==list(range(1,count+1))
    assert all(len(b['ticks'])<=128 for b in batches)
    assert len({b['stream_id'] for b in batches})==1
