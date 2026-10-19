"""Authenticated, request-scoped Dhan stream. Independent of collection worker.

No orders. No credential-bearing URLs/errors are emitted or persisted. LTT is
last-trade time; receipt of depth is NOT proof of exchange quote freshness.
"""
import asyncio
import json
import logging
import struct
import time
import uuid
from datetime import datetime,timezone
from urllib.parse import urlencode
from fastapi import HTTPException
import websockets
from collection import replace
from dhan_data import fetch_chain
from sdk_feeds import make_codec,decode_packet,SEGMENTS
from research import number,stamp

LOG=logging.getLogger('theta.private-live-socket')
LOG.handlers=[logging.NullHandler()];LOG.propagate=False;LOG.disabled=True

def now():return datetime.now(timezone.utc)

def split_packets(frame):
    if not isinstance(frame,bytes):raise ValueError('Binary feed required')
    position=0
    while position<len(frame):
        if len(frame)-position<8:raise ValueError('Truncated feed header')
        size=int.from_bytes(frame[position+1:position+3],'little')
        if size<8 or position+size>len(frame):raise ValueError('Invalid feed length')
        yield frame[position:position+size]
        position+=size

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
    # SDK utc_time() discards the date. Preserve the original full epoch instead.
    epoch=struct.unpack_from('<I',packet,12 if code==2 else 14)[0]
    traded=stamp(epoch);price=number(decoded.get('LTP'))
    if price is None or price<=0:return None
    age=(received-traded).total_seconds() if traded else None
    state='TIME_UNVERIFIED' if traded is None else 'FUTURE_TIMESTAMP' if age<0 else 'RECENT_TRADE' if age<=10 else 'STALE_TRADE'
    depth=(decoded.get('depth') or [{}])[0]
    bid,ask=number(depth.get('bid_price')),number(depth.get('ask_price'))
    valid=bid is not None and ask is not None and 0<bid<=ask
    return {'type':'tick',**identity,'ltp':price,'trade_at':traded.isoformat() if traded else None,
            'received_at':received.isoformat(),'trade_state':state,
            'bid':bid,'ask':ask,'spread_pct':100*(ask-bid)/((ask+bid)/2) if valid else None,
            'volume':number(decoded.get('volume')),'oi':number(decoded.get('OI')),
            'average_trade_price':number(decoded.get('avg_price')),
            'quote_state':'EXCHANGE_QUOTE_TIME_UNAVAILABLE','quote_at':None,
            'time_basis':'LTT is last trade time; depth has server receipt only',
            'signal_eligible':False}

async def prepare(board,provider,store,symbol):
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
            for strike,key in strikes[max(0,nearest-2):nearest+3]:
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
    return instruments

def claim(store):
    owner=uuid.uuid4().hex;claimed=False
    def acquire(old):
        nonlocal claimed
        claimed=False
        if old and old.get('until',0)>time.time():return old
        claimed=True;return {'owner':owner,'until':time.time()+300}
    replace(store,'research/live-lease',acquire)
    if not claimed:raise HTTPException(409,'A live research stream is already open. Stop that tab or wait for its lease to expire.')
    return owner

def release(store,owner):
    replace(store,'research/live-lease',lambda old:None if old and old.get('owner')==owner else old)

def event(data):return 'data: '+json.dumps(data,allow_nan=False,separators=(',',':'))+'\n\n'

async def stream(provider,store,board,symbol,request):
    owner=None
    try:
        deadline=time.monotonic()+240
        owner=await asyncio.to_thread(claim,store)
        yield event({'type':'status','state':'CONNECTING','received_at':now().isoformat()})
        instruments=await prepare(board,provider,store,symbol)
        codec=await asyncio.to_thread(make_codec,provider.credentials,'live',instruments)
        lookup={(SEGMENTS[i['segment']],str(i['security_id'])):i for i in instruments}
        params=urlencode({'version':'2','token':provider.credentials.token(),'clientId':provider.credentials.client_id,'authType':'2'})
        async with websockets.connect('wss://api-feed.dhan.co?'+params,open_timeout=10,close_timeout=2,
                                     ping_interval=20,ping_timeout=20,max_size=2**20,logger=LOG) as socket:
            codec.ws=socket;await codec.subscribe_instruments()
            yield event({'type':'status','state':'CONNECTED_AWAITING_TICKS','instruments':instruments,'received_at':now().isoformat(),
                         'scope':'While this page is connected; automatic 240s renewal. Not an unattended recorder.'})
            next_emit=0.;pending={};last_tick=None
            while time.monotonic()<deadline:
                if await request.is_disconnected():return
                try:
                    frame=await asyncio.wait_for(socket.recv(),timeout=1)
                    for packet in split_packets(frame):
                        row=observation(codec,packet,lookup,now())
                        if row and row['type']=='status':yield event(row);return
                        if row:pending[(row['segment'],row['security_id'])]=row;last_tick=now().isoformat()
                except asyncio.TimeoutError:pass
                if time.monotonic()>=next_emit:
                    yield event({'type':'batch','ticks':list(pending.values()),'received_at':now().isoformat(),'last_tick_received_at':last_tick})
                    pending.clear();next_emit=time.monotonic()+1
            yield event({'type':'status','state':'RENEW_CONNECTION','received_at':now().isoformat()})
    except asyncio.CancelledError:raise
    except HTTPException as exc:
        yield event({'type':'status','state':'UNAVAILABLE','message':exc.detail})
    except Exception:
        yield event({'type':'status','state':'UNAVAILABLE','message':'Dhan live connection unavailable. Check saved token, data entitlement and market session.'})
    finally:
        if owner:await asyncio.to_thread(release,store,owner)
