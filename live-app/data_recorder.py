"""Unattended bounded-memory recording with immutable chunks and explicit gaps.

Receipt timestamps never become exchange timestamps. Missing ticks cannot be
reconstructed from REST candles. No trading/order methods are used.
"""
import asyncio
import base64
import gzip
import json
import logging
import struct
import time
import uuid
from datetime import datetime,timezone
from urllib.parse import urlencode
import websockets
from collection import replace,install_once
from sdk_feeds import make_codec,decode_packet,SEGMENTS,split_live_packets
from live_research import observation,number
from diagnostics import capture
from provider import ProviderError

LIMITS={'live':5000,'depth20':50,'depth200':1}
LOG=logging.getLogger('theta.data-recorder.private')
LOG.handlers=[logging.NullHandler()];LOG.propagate=False;LOG.disabled=True

def utcnow():return datetime.now(timezone.utc)

def packets(frame,family):
    if family=='live':
        yield from split_live_packets(frame)
        return
    if not isinstance(frame,bytes):raise ValueError('Expected binary market data')
    position=0;minimum=8 if family=='live' else 12
    while position<len(frame):
        part=frame[position:]
        if len(part)<minimum:raise ValueError('Truncated market data header')
        size=int.from_bytes(part[1:3] if family=='live' else part[:2],'little')
        if size<minimum or size>len(part):raise ValueError('Invalid market data packet length')
        yield part[:size];position+=size

class Journal:
    def __init__(self,store,family,instruments,revision,clock=utcnow,stream_id=None):
        self.store=store;self.family=family;self.instruments=instruments;self.revision=revision;self.clock=clock
        self.stream_id=stream_id or family
        self.session_id=uuid.uuid4().hex;self.sequence=0;self.frames=[];self.bytes=0;self.latest={};self.decode_errors=0
        self.expected={(SEGMENTS[i['segment']],str(i['security_id'])):i for i in instruments}
        self.previous={}
        self.observed=set();self.first=None;self.last=None;self.framing_adjustments=0

    def append(self,frame,codec):
        received=self.clock();stamp=received.isoformat();ticks=[]
        self.frames.append({'received_at':stamp,'binary_base64':base64.b64encode(frame).decode()})
        self.bytes+=len(frame);self.first=self.first or stamp;self.last=stamp
        for packet in packets(frame,self.family):
            if self.family=='live' and packet[0]==8 and int.from_bytes(packet[1:3],'little')==70:
                self.framing_adjustments+=1
            offset=0 if self.family=='live' else 2
            code=packet[offset]
            if code==50:raise ProviderError('Provider disconnected',code=str(int.from_bytes(packet[8:10],'little')) if len(packet)>=10 else None)
            identity=(packet[3],str(int.from_bytes(packet[4:8],'little')))
            if identity not in self.expected:continue
            try:
                decoded=decode_packet(codec,packet)
                if not isinstance(decoded,dict) or not decoded:self.decode_errors+=1;continue
                self.observed.add(identity)
                if self.family=='live':
                    if code==6:
                        previous=number(decoded.get('prev_close'))
                        if previous is not None and previous>0:
                            meta={'previous_close':previous,'previous_close_received_at':stamp}
                            self.previous[identity]=meta
                            key=str(identity[0])+':'+identity[1]
                            if key in self.latest:self.latest[key]={**self.latest[key],**meta}
                        continue # Previous-close packets never become price ticks.
                    row=observation(codec,packet,self.expected,received)
                    if row:row={**row,**self.previous.get(identity,{})}
                    if row and row.get('type')=='tick':ticks.append(row)
                else:
                    row={**self.expected[identity],'received_at':stamp,'decoded':decoded,'quote_at':None,
                         'time_basis':'Server receipt only; exchange quote timestamp unavailable'}
                if row and row.get('type')!='status':
                    # Bid/ask depth arrive separately; keep both sides.
                    key=str(identity[0])+':'+identity[1]+(':'+str(decoded.get('type')) if self.family!='live' else '')
                    self.latest[key]=row
            except (ValueError,TypeError,KeyError,struct.error):self.decode_errors+=1
        return ticks

    def flush(self):
        if not self.frames:return None
        day=self.first[:10];key=f'streams/{day}/{self.stream_id}/{self.session_id}/{self.sequence:08d}'
        payload={'format':'gzip-json-base64','family':self.family,'session_id':self.session_id,'sequence':self.sequence,
                 'subscription_revision':self.revision,'first_received_at':self.first,'last_received_at':self.last,
                 'frames':len(self.frames),'bytes':self.bytes,'decode_errors':self.decode_errors,
                 'framing_adjustments':self.framing_adjustments,
                 'payload':base64.b64encode(gzip.compress(json.dumps(self.frames,separators=(',',':')).encode())).decode()}
        install_once(self.store,key,payload)
        now=self.clock().isoformat()
        status={'state':'RECORDING','heartbeat':now,'last_durable_received_at':self.last,'last_chunk':key,
                'session_id':self.session_id,'sequence':self.sequence,'subscription_revision':self.revision,
                'subscribed':len(self.instruments),'observed_since_connection':len(self.observed),
                'missing_since_connection':len(self.expected.keys()-self.observed),'decode_errors':self.decode_errors,
                'framing_adjustments':self.framing_adjustments,
                'continuity':'OBSERVED_CHUNKS_WITH_GAPS_REPORTED_NOT_CERTIFIED_LOSSLESS'}
        replace(self.store,'system/recorder/'+self.stream_id,lambda _:status)
        replace(self.store,'system/latest/'+self.stream_id,lambda _:{'received_at':self.last,'rows':self.latest})
        self.sequence+=1;self.frames=[];self.bytes=0;self.first=None
        return key

def gap(store,family,start,end,reason):
    if start and end:
        store.add('ingestion_gap',{'family':family,'from':start,'to':end,'reason':reason,
                                  'repair':'REST may repair candles; lost ticks/depth remain unavailable'})

async def record(provider,store,family,scope,enabled,clock=utcnow,connect=websockets.connect,sleep=asyncio.sleep,stream_id=None):
    name=stream_id or family
    retries=0
    while await asyncio.to_thread(enabled):
        plan=await asyncio.to_thread(scope,name)
        instruments=plan.get('instruments',[]);revision=plan.get('revision')
        if not instruments:
            await asyncio.to_thread(replace,store,'system/recorder/'+name,
                lambda old:{**(old or {}),'state':'NO_SUBSCRIPTION','heartbeat':clock().isoformat(),'subscribed':0})
            await sleep(10);continue
        if len(instruments)>LIMITS[family]:raise ValueError('Subscription capacity exceeded')
        journal=Journal(store,family,instruments,revision,clock,stream_id=name);connected_at=clock().isoformat()
        previous=(await asyncio.to_thread(store.document,'system/recorder/'+name))[0] or {}
        await asyncio.to_thread(gap,store,name,previous.get('last_durable_received_at'),connected_at,'CONNECTION_OR_PROCESS_RESTART')
        failure=False
        try:
            if hasattr(provider.credentials,'invalidate'):provider.credentials.invalidate()
            codec=await asyncio.to_thread(make_codec,provider.credentials,family,instruments)
            params=urlencode({'version':'2','token':provider.credentials.token(),'clientId':provider.credentials.client_id,'authType':'2'})
            host={'live':'wss://api-feed.dhan.co?','depth20':'wss://depth-api-feed.dhan.co/twentydepth?',
                  'depth200':'wss://full-depth-api.dhan.co/twohundreddepth?'}[family]
            async with connect(host+params,open_timeout=10,close_timeout=2,ping_interval=20,ping_timeout=20,
                               max_size=2**20,max_queue=4,logger=LOG) as socket:
                codec.ws=socket;await codec.subscribe_instruments()
                await asyncio.to_thread(replace,store,'system/recorder/'+name,lambda old:{**(old or {}),
                    'state':'CONNECTED_AWAITING_DATA','heartbeat':clock().isoformat(),'subscribed':len(instruments)})
                last_flush=time.monotonic();next_check=last_flush+15;last_frame=None;opened=last_flush
                while True:
                    try:
                        frame=await asyncio.wait_for(socket.recv(),1)
                        journal.append(frame,codec);last_frame=clock().isoformat();retries=0
                    except asyncio.TimeoutError:pass
                    now=time.monotonic()
                    if journal.bytes>=1024*1024 or now-last_flush>=2:
                        await asyncio.to_thread(journal.flush);last_flush=now
                    if now>=next_check:
                        if not await asyncio.to_thread(enabled):break
                        current=await asyncio.to_thread(scope,name)
                        await asyncio.to_thread(replace,store,'system/recorder/'+name,lambda old:{**(old or {}),
                            'heartbeat':clock().isoformat(),'last_frame_received_at':last_frame,
                            'state':'RECORDING' if last_frame else 'CONNECTED_AWAITING_DATA'})
                        next_check=now+15
                        if current.get('revision')!=revision:break
                    # Reload saved credentials on renewal; failure never changes checkpoints.
                    if now-opened>=900:break
        except asyncio.CancelledError:raise
        except Exception as error:
            await asyncio.to_thread(capture,store,'feed_'+name,error)
            # Neither SDK errors nor connection exceptions containing auth URLs escape.
            retries+=1
            failure=True
        finally:
            # A failed durable write is not discarded. Propagate it to supervisor,
            # which records an outage; bounded socket backpressure is deliberate.
            await asyncio.shield(asyncio.to_thread(journal.flush))
            if failure:
                await asyncio.to_thread(replace,store,'system/recorder/'+name,lambda old:{**(old or {}),
                    'state':'DISCONNECTED_RETRYING','heartbeat':clock().isoformat(),'reconnect_attempt':retries,
                    'error':'Provider, credentials or storage unavailable; raw checkpoints retained'})
        if await asyncio.to_thread(enabled):await sleep(min(60,2**min(retries,6)))
