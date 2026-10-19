"""Current official membership + Dhan last-trade participation, never historical weights."""
import asyncio
import csv
import hashlib
import io
import re
from datetime import datetime,timezone
import httpx
from provider import ProviderError
from market_board import quote_time,number
from runtime import IST

SOURCES={
    'nifty':'https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv',
    'banknifty':'https://www.niftyindices.com//IndexConstituent/ind_niftybanklist.csv',
}

def parse_members(text):
    reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'Symbol','Series','ISIN Code'}.issubset(reader.fieldnames or []):raise ValueError('Membership format unavailable')
    rows=[];seen=set()
    for row in reader:
        symbol=row['Symbol'].strip().upper();isin=row['ISIN Code'].replace(' ','').strip().upper()
        if row['Series'].strip()!='EQ' or not re.fullmatch(r'[A-Z0-9&-]{1,40}',symbol) or not re.fullmatch(r'IN[A-Z0-9]{10}',isin) or isin in seen:
            raise ValueError('Invalid or duplicate member identity')
        seen.add(isin);rows.append({'symbol':symbol,'isin':isin})
    if not 5<=len(rows)<=100:raise ValueError('Membership count unavailable')
    return rows

def parse_equities(text):
    result={}
    for row in csv.DictReader(io.StringIO(text.lstrip('\ufeff'))):
        if (row.get('EXCH_ID'),row.get('SEGMENT'),row.get('INSTRUMENT'),row.get('SERIES'))!=('NSE','E','EQUITY','EQ'):continue
        isin=(row.get('ISIN') or '').strip().upper();sid=row.get('SECURITY_ID','')
        if re.fullmatch(r'IN[A-Z0-9]{10}',isin) and sid.isdigit():
            result.setdefault(isin,[]).append({'security_id':sid,'master_name':row['SYMBOL_NAME'].strip()})
    return result

async def download_members(url):
    async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
        async with client.stream('GET',url) as response:
            response.raise_for_status();raw=bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw)>100000:raise ValueError('Membership download exceeded bound')
    return raw.decode('utf-8-sig')

def view(record,clock):
    from research import stamp
    if not record:return {'state':'NOT_CONNECTED','fraction_up':None,'fraction_down':None}
    result={k:v for k,v in record.items() if k!='raw'}
    received=stamp(record.get('received_at'));membership=stamp(record.get('membership_received_at'))
    rows=record.get('rows',[])
    local=clock.astimezone(IST)
    in_session=local.weekday()<5 and '09:15'<=local.strftime('%H:%M')<'15:30'
    valid=bool(in_session and rows and received and membership and 0<=(clock-received).total_seconds()<=60 and
               membership<=clock and membership.astimezone(IST).date()==clock.astimezone(IST).date())
    valid=valid and all((at:=stamp(r.get('trade_at'))) is not None and 0<=(clock-at).total_seconds()<=60 and
                       r.get('direction') in ('up','down','flat') for r in rows)
    if valid:
        up=sum(r['direction']=='up' for r in rows);down=sum(r['direction']=='down' for r in rows)
        result.update(state='RECENT_TRADE_PARTICIPATION',fraction_up=up/len(rows),fraction_down=down/len(rows),advances=up,declines=down,unchanged=len(rows)-up-down)
    else:result.update(state='INCOMPLETE_OR_STALE' if in_session else 'OUTSIDE_REGULAR_SESSION',fraction_up=None,fraction_down=None)
    return result

async def refresh(provider,store,board,symbol,clock=None):
    from research import stamp
    from collection import replace
    clock=clock or datetime.now(timezone.utc);key='research/breadth/'+symbol
    old,_=await asyncio.to_thread(store.document,key);at=stamp((old or {}).get('received_at'))
    if old and at and 0<=(clock-at).total_seconds()<55:return view(old,clock)
    local=clock.astimezone(IST)
    if local.weekday()>4 or not '09:15'<=local.strftime('%H:%M')<'15:30':
        return {**view(old,clock),'state':'OUTSIDE_REGULAR_SESSION','fraction_up':None,'fraction_down':None}
    await board.catalog()
    membership_key='research/membership/'+symbol
    membership,_=await asyncio.to_thread(store.document,membership_key)
    membership_at=stamp((membership or {}).get('received_at'))
    try:
        if not membership_at or membership_at>clock or membership_at.astimezone(IST).date()!=local.date():
            text=await download_members(SOURCES[symbol]);members=parse_members(text)
            if symbol=='nifty' and len(members)!=50:raise ValueError('NIFTY 50 membership is incomplete')
            membership={'source':SOURCES[symbol],'received_at':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(text.encode()).hexdigest(),'members':members}
            await asyncio.to_thread(replace,store,membership_key,lambda _:membership)
        resolved=[]
        for member in membership['members']:
            matches=getattr(board,'equities',{}).get(member['isin'],[])
            if len(matches)!=1:raise ValueError('Exact constituent ISIN/series identity unavailable')
            resolved.append({**member,**matches[0]})
        raw=await provider.post('marketfeed/quote',{'NSE_EQ':[int(r['security_id']) for r in resolved]})
        payload=raw.get('data',raw)
        if not isinstance(payload,dict):raise ValueError('Quote payload unavailable')
        quotes=payload.get('NSE_EQ',{})
        if not isinstance(quotes,dict):raise ValueError('Quote basket unavailable')
        rows=[]
        for member in resolved:
            quote=quotes.get(member['security_id']) or {}
            if not isinstance(quote,dict):quote={}
            ohlc=quote.get('ohlc') or {}
            price=number(quote.get('last_price'));previous=number(ohlc.get('close')) if isinstance(ohlc,dict) else None
            traded=quote_time(quote.get('last_trade_time'),clock)
            rows.append({**member,'ltp':price,'previous_close':previous,'trade_at':traded.isoformat() if traded else None,
                         'direction':None if price is None or previous is None or price<=0 or previous<=0 else 'up' if price>previous else 'down' if price<previous else 'flat'})
        record={'symbol':symbol,'received_at':datetime.now(timezone.utc).isoformat(),'membership_received_at':membership['received_at'],
                'membership_source':membership['source'],'membership_sha256':membership['sha256'],'rows':rows,'raw':raw,
                'basis':'Equal-count participation versus previous close; last trade times, not exchange quote times. Not index weights. Current membership only; never replayed into history.'}
        await asyncio.to_thread(replace,store,key,lambda _:record)
        return view(record,datetime.now(timezone.utc))
    except ProviderError as exc:return {'state':'PROVIDER_ERROR','fraction_up':None,'fraction_down':None,'reason':str(exc)}
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        return {'state':'MEMBERSHIP_OR_IDENTITY_UNAVAILABLE','fraction_up':None,'fraction_down':None,'reason':'Official current constituents or exact ISIN/series match unavailable; no substitute basket used.'}
