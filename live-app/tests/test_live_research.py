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
    return struct.pack('<BHBIfI',2,16,0,sid,price,int((epoch or at()).timestamp()))

def instruments():return {(0,'13'):{'segment':'IDX_I','security_id':'13','key':'nifty','role':'underlying','label':'NIFTY'}}

def test_epoch_keeps_date_not_just_sdk_time_and_never_implies_quote_time():
    row=live.observation(codec(),ticker(),instruments(),at()+timedelta(seconds=1))
    assert row['trade_at']=='2026-10-19T08:00:00+00:00'
    assert row['trade_state']=='RECENT_TRADE' and row['ltp']==100
    assert row['quote_at'] is None and not row['signal_eligible']

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
    packet=struct.pack('<BHBIfHIfIIIIIIffff100s',8,162,2,123,100,10,int(at().timestamp()),100,1000,100,200,500,501,499,100,99,101,98,depth)
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

def test_live_route_requires_existing_admin_and_rejects_unknown_symbols(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('K_SERVICE','THETA_GCS_BUCKET','THETA_TOKEN_ADMIN_HASH'):monkeypatch.delenv(key,raising=False)
    c=TestClient(server.create_app(tmp_path/'app.db'))
    assert c.post('/api/research/live',json={'symbol':'nifty'}).status_code==503
    assert c.post('/api/research/live',json={'symbol':'arbitrary-security'}).status_code==422
