"""Official SDK subscriptions and decoders inside bounded raw-feed capture."""
import json
from credentials import CredentialError
from dhanhq import DhanContext
from dhanhq.marketfeed import MarketFeed
from dhanhq.fulldepth import FullDepth
from provider import ProviderError,SDK_VERSION

SEGMENTS={'IDX_I':0,'NSE_EQ':1,'NSE_FNO':2,'NSE_CURRENCY':3,'BSE_EQ':4,
          'MCX_COMM':5,'BSE_CURRENCY':7,'BSE_FNO':8}


def split_live_packets(frame):
    """Keep wire bytes intact, including the observed Full header length variant.

    Dhan's Full decoder consumes exactly 162 bytes. Saved production frames also
    contain a Full header declaring 70 for that same 162-byte layout. Accept only
    this confirmed code/length pair; other invalid lengths remain errors.
    """
    if not isinstance(frame,bytes):raise ValueError('Binary feed required')
    position=0
    while position<len(frame):
        if len(frame)-position<8:raise ValueError('Truncated feed header')
        code=frame[position];size=int.from_bytes(frame[position+1:position+3],'little')
        if code==8 and size==70:size=162
        if size<8 or position+size>len(frame):raise ValueError('Invalid feed length')
        yield frame[position:position+size]
        position+=size


def make_codec(credentials,family,instruments):
    """Called in a thread: SDK constructors otherwise replace the ASGI event loop."""
    try:context=DhanContext(credentials.client_id,credentials.token())
    except CredentialError:raise ProviderError('Dhan credentials unavailable',code='DH-901',status=401) from None
    tuples=[]
    for item in instruments:
        segment=SEGMENTS.get(item['segment'])
        if segment is None or family!='live' and segment not in (1,2):
            raise ProviderError('Instrument segment is unsupported by this SDK feed')
        # Spot indices have no depth/OI and are not delivered in Full mode.
        mode=MarketFeed.Ticker if segment==0 else MarketFeed.Full
        tuples.append((segment,item['security_id'],mode) if family=='live' else (segment,item['security_id']))
    try:
        codec=MarketFeed(context,tuples,version='v2') if family=='live' else FullDepth(context,tuples,depth_level=20 if family=='depth20' else 200)
        codec.loop.close()  # Theta owns the async lifecycle and bounded socket.
        return codec
    finally:context.get_dhan_http().session.close()


def decode_packet(codec,packet):
    result=codec.process_data(packet)
    if isinstance(result,dict):result={k:v for k,v in result.items() if k!='remaining_data'}
    json.dumps(result,allow_nan=False)
    return result


def feed_info(family):
    return {'sdk':'dhanhq','sdk_version':SDK_VERSION,'decoder':'MarketFeed' if family=='live' else 'FullDepth'}
