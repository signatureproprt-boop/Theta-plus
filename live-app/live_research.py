"""Authenticated, request-scoped Dhan stream. Independent of collection worker.

No orders. No credential-bearing URLs/errors are emitted or persisted. LTT is
last-trade time; receipt of depth is NOT proof of exchange quote freshness.
"""
import asyncio
import anyio
import json
import logging
import struct
import time
import uuid
from datetime import datetime,timezone,timedelta
from urllib.parse import urlencode
from fastapi import HTTPException
import websockets
from collection import replace
from dhan_data import fetch_chain
from sdk_feeds import make_codec,decode_packet,SEGMENTS
from research import number,stamp
from runtime import IST

LOG=logging.getLogger('theta.private-live-socket')
LOG.handlers=[logging.NullHandler()];LOG.propagate=False;LOG.disabled=True

def now():return datetime.now(timezone.utc)

def split_packets(frame):
    from sdk_feeds import split_live_packets
    yield from split_live_packets(frame)

def observation(codec,packet,instruments,received):
    code=packet[0]
    if code==50:
        return {'type':'status','state':'PROVIDER_DISCONNECT','code':int.from_bytes(packet[8:10],'little') if len(packet)>=10 else None}
    if code not in (2,4,8):return None
    segment=packet[3];sid=str(int.from_bytes(packet[4:8],'little'))
    identity=instruments.get((segment,sid))
    if identity is None:return None
    minimum=16 if code==2 else 50 if code==4 else 162
    if len(packet)<minimum:raise ValueError('Truncated price packet')
    decoded=decode_packet(codec,packet)
    if not isinstance(decoded,dict):return None
    # Dhan support confirms live LTT uses an IST wall-clock epoch. The SDK's
    # utc_time() returns that clock without a date. Preserve raw epoch and apply
    # this vendor-specific convention ONLY here, never to historical candles.
    # https://madefortrade.in/t/57896/2
    epoch=struct.unpack_from('<I',packet,12 if code==2 else 14)[0]
    raw_time=stamp(epoch)
    traded=raw_time-timedelta(seconds=19800) if raw_time else None
    price=number(decoded.get('LTP'))
    if price is None or price<=0:return None
    age=(received-traded).total_seconds() if traded else None
    state='TIME_UNVERIFIED' if traded is None else 'FUTURE_TIMESTAMP' if age<0 else 'RECENT_TRADE' if age<=10 else 'STALE_TRADE'
    depth=(decoded.get('depth') or [{}])[0]
    bid,ask=number(depth.get('bid_price')),number(depth.get('ask_price'))
    valid=bid is not None and ask is not None and 0<bid<=ask
    return {'type':'tick',**identity,'ltp':price,'trade_at':traded.isoformat() if traded else None,
            'raw_ltt_epoch':epoch,'timestamp_convention':'Dhan live LTT IST wall-clock epoch; UTC = raw epoch - 19800s',
            'received_at':received.isoformat(),'trade_state':state,
            'bid':bid,'ask':ask,'spread_pct':100*(ask-bid)/((ask+bid)/2) if valid else None,
            'volume':number(decoded.get('volume')),'oi':number(decoded.get('OI')),
            'average_trade_price':number(decoded.get('avg_price')),
            'quote_state':'EXCHANGE_QUOTE_TIME_UNAVAILABLE','quote_at':None,
            'time_basis':'LTT is last trade time; depth has server receipt only',
            'signal_eligible':False}

async def prepare(board,provider,store,symbol,contract_id=''):
    await board.catalog()
    instruments=[]
    for key in ('nifty','banknifty','india_vix','nifty_future','banknifty_future'):
        item=board.instruments.get(key)
        if item:instruments.append({**item,'label':item.get('label',key),'role':'underlying' if key==symbol else 'context'})
    target=board.instruments.get(symbol)
    if not target:raise HTTPException(503,'Underlying is not resolved in the current Dhan master')
    # One current-expiry chain per connection. No automatic PCR history claims.
    try:
        saved=await fetch_chain(provider,store,symbol,target)
        record=await asyncio.to_thread(store.get,'dhan_chain',saved['id'])
        data=record['data']['raw'].get('data',{})
        pairs=data.get('oc',{});spot=number(data.get('last_price'))
        strikes=sorted((number(k),k) for k in pairs if number(k) is not None)
        if spot and strikes:
            nearest=min(range(len(strikes)),key=lambda i:abs(strikes[i][0]-spot))
            for strike,key in strikes[max(0,nearest-5):nearest+6]:
                for leg,side in (('ce','CE'),('pe','PE')):
                    raw=pairs[key].get(leg) or {};sid=str(raw.get('security_id',''))
                    master=board.options.get(sid)
                    if not master or master['expiry']!=saved['expiry'] or master['strike']!=strike or master['side']!=side:continue
                    instruments.append({**master,'key':sid,'segment':'NSE_FNO','label':f'{symbol.upper()} {strike:g} {side}',
                                        'role':'option','underlying':symbol,'identity_verified':True})
    except HTTPException:raise
    except Exception:
        # Index stream can still operate if option entitlement/expiry lookup fails.
        pass
    # Keep an explicitly selected exact contract subscribed even outside the
    # current ATM window. Master identity, underlying and expiry must match.
    selected=board.options.get(contract_id)
    if selected and selected.get('underlying')==symbol and selected.get('expiry','')>=now().astimezone(IST).date().isoformat() and not any(i['security_id']==contract_id and i.get('segment')=='NSE_FNO' for i in instruments):
        instruments.append({**selected,'key':contract_id,'segment':'NSE_FNO','label':f"{symbol.upper()} {selected['strike']:g} {selected['side']}",
                            'role':'option','underlying':symbol,'identity_verified':True})
    return instruments

def claim(store):
    owner=uuid.uuid4().hex;claimed=False
    def acquire(old):
        nonlocal claimed
        claimed=False
        if old and old.get('until',0)>time.time():return old
        claimed=True;return {'owner':owner,'until':time.time()+300}
    replace(store,'research/live-lease',acquire)
    if not claimed:raise HTTPException(409,'A live research stream is already open. Waiting for its lease to release; Stop cancels this retry.')
    return owner

def release(store,owner):
    replace(store,'research/live-lease',lambda old:None if old and old.get('owner')==owner else old)

def event(data):return 'data: '+json.dumps(data,allow_nan=False,separators=(',',':'))+'\n\n'

async def stream(provider,store,board,symbol,request,contract_id=''):
    owner=None
    try:
        deadline=time.monotonic()+240
        # ASGI disconnects use cancellation scopes. Complete ownership assignment
        # before cancellation can enter; otherwise the claimed owner is lost.
        with anyio.CancelScope(shield=True):
            owner=await anyio.to_thread.run_sync(claim,store)
        yield event({'type':'status','state':'CONNECTING','received_at':now().isoformat()})
        instruments=await prepare(board,provider,store,symbol,contract_id)
        codec=await asyncio.to_thread(make_codec,provider.credentials,'live',instruments)
        lookup={(SEGMENTS[i['segment']],str(i['security_id'])):i for i in instruments}
        params=urlencode({'version':'2','token':provider.credentials.token(),'clientId':provider.credentials.client_id,'authType':'2'})
        async with websockets.connect('wss://api-feed.dhan.co?'+params,open_timeout=10,close_timeout=2,
                                     ping_interval=20,ping_timeout=20,max_size=2**20,logger=LOG) as socket:
            codec.ws=socket;await codec.subscribe_instruments()
            yield event({'type':'status','state':'CONNECTED_AWAITING_TICKS','instruments':instruments,'received_at':now().isoformat(),
                         'scope':'While this page is connected; automatic 240s renewal. Not an unattended recorder.'})
            next_emit=0.;pending=[];last_tick=None;sequence=0;stream_id=uuid.uuid4().hex
            def batch():
                return {'type':'batch','ticks':list(pending),'received_at':now().isoformat(),
                        'last_tick_received_at':last_tick,'stream_id':stream_id,
                        'last_sequence':sequence,'transport':'Ordered decoded provider price observations; UI painting may coalesce. Upstream tick loss is not certified.'}
            while time.monotonic()<deadline:
                if await request.is_disconnected():return
                try:
                    frame=await asyncio.wait_for(socket.recv(),timeout=1)
                    for packet in split_packets(frame):
                        row=observation(codec,packet,lookup,now())
                        if row and row['type']=='status':
                            if pending:yield event(batch());pending.clear()
                            yield event(row);return
                        if row:
                            sequence+=1;pending.append({**row,'stream_sequence':sequence});last_tick=now().isoformat()
                            # Keep every decoded observation in order, bounded
                            # below the browser's per-event size limit.
                            if len(pending)>=128:yield event(batch());pending.clear()
                except asyncio.TimeoutError:pass
                if time.monotonic()>=next_emit:
                    yield event(batch())
                    pending.clear();next_emit=time.monotonic()+.1
            if pending:yield event(batch())
            yield event({'type':'status','state':'RENEW_CONNECTION','received_at':now().isoformat()})
    except asyncio.CancelledError:raise
    except HTTPException as exc:
        yield event({'type':'status','state':'STREAM_BUSY' if exc.status_code==409 else 'UNAVAILABLE',
                     'retry_after_seconds':5 if exc.status_code==409 else None,'message':exc.detail})
    except Exception:
        yield event({'type':'status','state':'UNAVAILABLE','message':'Dhan live connection unavailable. Check saved token, data entitlement and market session.'})
    finally:
        if owner:
            # Keep cleanup alive under Starlette/AnyIO's disconnect cancellation.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(release,store,owner)
