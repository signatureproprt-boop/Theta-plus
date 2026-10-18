"""Dhan v2 ticker relay. Credentials stay server-side; browser uses bearer auth."""
import asyncio
import json
import math
import os
import struct
import time
from datetime import datetime,timezone
from urllib.parse import urlencode
from websockets.asyncio.client import connect

SEGMENTS={0:'IDX_I',1:'NSE_EQ',2:'NSE_FNO',3:'NSE_CURRENCY',4:'BSE_EQ',5:'MCX_COMM',7:'BSE_CURRENCY',8:'BSE_FNO'}

def decode_frame(raw):
    if not isinstance(raw,bytes):raise ValueError('Expected binary feed')
    events=[];offset=0
    while offset<len(raw):
        if len(raw)-offset<8:raise ValueError('Truncated header')
        code,length,segment,sid=struct.unpack_from('<BHBI',raw,offset)
        if length<8 or offset+length>len(raw):raise ValueError('Invalid packet length')
        p=raw[offset:offset+length];offset+=length
        if code==50:
            if length<10:raise ValueError('Truncated disconnect')
            events.append({'type':'rejected','code':struct.unpack_from('<H',p,8)[0]});continue
        if code not in (2,6):continue
        if length!=16:raise ValueError('Invalid ticker length')
        value,stamp=struct.unpack_from('<fI',p,8)
        if not math.isfinite(value) or value<=0:continue
        event={'type':'tick' if code==2 else 'previous_close','segment':SEGMENTS.get(segment),'security_id':str(sid),'value':value}
        if code==2:event['epoch']=stamp
        events.append(event)
    return events

def line(data):return json.dumps(data,allow_nan=False,separators=(',',':'))+'\n'

async def relay(request,instruments,credentials=None):
    mapping={(i['segment'],i['security_id']):i for i in instruments}
    previous={};received=False
    if credentials is None:
        from credentials import DhanCredentials
        credentials=DhanCredentials()
    try:query=urlencode({'version':'2','token':credentials.token(),'clientId':credentials.client_id,'authType':'2'})
    except Exception:
        yield line({'type':'state','state':'TOKEN_UNAVAILABLE'});return
    # Never expose URL, upstream exceptions, or credentials in output/log messages.
    yield line({'type':'state','state':'CONNECTING'})
    try:
        async with connect('wss://api-feed.dhan.co?'+query,open_timeout=20,close_timeout=5,
                           ping_interval=20,ping_timeout=20,max_size=1048576) as ws:
            await ws.send(json.dumps({'RequestCode':15,'InstrumentCount':len(instruments),
                'InstrumentList':[{'ExchangeSegment':i['segment'],'SecurityId':i['security_id']} for i in instruments]}))
            yield line({'type':'state','state':'CONNECTED_WAITING_FOR_TICKS'})
            deadline=time.monotonic()+900
            while time.monotonic()<deadline:
                if await request.is_disconnected():return
                try:raw=await asyncio.wait_for(ws.recv(),10)
                except asyncio.TimeoutError:
                    yield line({'type':'heartbeat','state':'CONNECTED_WAITING_FOR_TICKS' if not received else 'CONNECTED','at':datetime.now(timezone.utc).isoformat()});continue
                for event in decode_frame(raw):
                    if event['type']=='rejected':
                        yield line({'type':'state','state':'REJECTED','code':event['code']});return
                    identity=(event['segment'],event['security_id'])
                    instrument=mapping.get(identity)
                    if not instrument:continue
                    if event['type']=='previous_close':previous[identity]=event['value'];continue
                    now=datetime.now(timezone.utc);epoch=event['epoch'];at=None
                    if 1577836800<=epoch<=now.timestamp():at=datetime.fromtimestamp(epoch,timezone.utc)
                    prev=previous.get(identity);price=event['value'];received=True
                    yield line({'type':'tick','key':instrument['key'],'security_id':instrument['security_id'],
                        'segment':instrument['segment'],'expiry':instrument['expiry'],'price':price,
                        'change':price-prev if prev else None,'change_pct':100*(price-prev)/prev if prev else None,
                        'source':'DhanHQ WebSocket','quote_at':at.isoformat() if at else None,
                        'fetched_at':now.isoformat(),'status':'TIME_UNAVAILABLE' if at is None else 'RECENT_TRADE' if (now-at).total_seconds()<=60 else 'STALE'})
            yield line({'type':'state','state':'RECONNECTING'})
    except asyncio.CancelledError:raise
    except Exception:
        yield line({'type':'state','state':'DISCONNECTED','message':'Dhan streaming connection failed. Check token, data access and connection limits.'})
