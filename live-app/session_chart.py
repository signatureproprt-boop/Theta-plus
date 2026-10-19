"""Bounded current-session OHLC refresh, independent of collection and socket leases."""
import asyncio
from datetime import datetime,timedelta,timezone
from dhan_data import candle_count
from provider import ProviderError
from research import stamp,freshness
from runtime import IST

def session_view(record,clock):
    day=clock.astimezone(IST).date()
    start=datetime.combine(day,datetime.min.time(),IST).replace(hour=9,minute=15)
    end=start.replace(hour=15,minute=30)
    cutoff=min(clock.replace(second=0,microsecond=0),end)
    expected=set(range(int(start.timestamp()),max(int(start.timestamp()),int(cutoff.timestamp())),60)) if day.weekday()<5 else set()
    raw=record['raw'];candle_count(raw)
    rows={};conflicts=set();excluded=0
    for i,t in enumerate(raw['timestamp']):
        if t not in expected:excluded+=1;continue
        row={k:raw[k][i] for k in ('timestamp','open','high','low','close','volume')}
        row.update(closed_at=stamp(t+60).isoformat(),received_at=record['received_at'])
        if t in rows and any(rows[t][k]!=row[k] for k in ('open','high','low','close','volume')):conflicts.add(t)
        else:rows[t]=row
    candles=[r for t,r in sorted(rows.items()) if t not in conflicts]
    missing=sorted(expected-{r['timestamp'] for r in candles})
    status=freshness(candles[-1]['closed_at'] if candles else None,record['received_at'],clock,90)
    return {'symbol':record['symbol'],'session_date':day.isoformat(),'server_time':clock.isoformat(),
            'received_at':record['received_at'],'candles':candles,'freshness':status,
            'expected_minutes':len(expected),'missing_minutes':len(missing),'missing_timestamps':missing,
            'conflicting_minutes':len(conflicts),'excluded_rows':excluded,
            'calendar':'Weekday regular session only; exchange holidays and special sessions not certified',
            'source':'Dhan charts/intraday · provider OHLC; no candles reconstructed from sampled live ticks',
            'signal_eligible':False}

async def refresh_session(provider,store,board,symbol,clock=None):
    fixed_clock=clock is not None
    clock=clock or datetime.now(timezone.utc)
    await board.catalog()
    item=board.instruments.get(symbol)
    if not item or item.get('segment')!='IDX_I' or item.get('instrument')!='INDEX':
        raise ProviderError('Current master has no exact index identity')
    key='research/session/'+symbol
    old,_=await asyncio.to_thread(store.document,key)
    fetched=stamp((old or {}).get('received_at'))
    day=clock.astimezone(IST).date()
    if old and old.get('session_date')==day.isoformat() and old.get('security_id')==str(item['security_id']) and fetched and 0<=(clock-fetched).total_seconds()<55:
        return session_view(old,clock)
    start=datetime.combine(day,datetime.min.time(),IST).replace(hour=9,minute=15)
    end=min(clock.astimezone(IST).replace(second=0,microsecond=0),start.replace(hour=15,minute=30))
    if day.weekday()>4 or end<=start:
        raw={k:[] for k in ('timestamp','open','high','low','close','volume')};body=None
    else:
        body={'securityId':str(item['security_id']),'exchangeSegment':'IDX_I','instrument':'INDEX',
              'interval':'1','oi':False,'fromDate':start.strftime('%Y-%m-%d %H:%M:%S'),'toDate':end.strftime('%Y-%m-%d %H:%M:%S')}
        raw=await provider.post('charts/intraday',body)
        candle_count(raw)
    received=clock if fixed_clock else datetime.now(timezone.utc)
    record={'symbol':symbol,'security_id':str(item['security_id']),'session_date':day.isoformat(),
            'received_at':received.isoformat(),'request':body,'raw':raw}
    # Replace one bounded document. Do not append 375-bar snapshots to legacy history.
    from collection import replace
    await asyncio.to_thread(replace,store,key,lambda previous:record if not previous or (stamp(previous.get('received_at')) or datetime.min.replace(tzinfo=timezone.utc))<=received else previous)
    return session_view(record,received)

async def refresh_context(provider,store,board,symbol):
    """One daily 21-calendar-day warm-up plus current-front future session data."""
    from collection import replace
    clock=datetime.now(timezone.utc);local=clock.astimezone(IST);day=local.date()
    await board.catalog()
    start=local.replace(hour=9,minute=15,second=0,microsecond=0)
    index=board.instruments.get(symbol)
    key='research/warmup/'+symbol;old=await asyncio.to_thread(store.document,key)
    report={}
    if index and (not old[0] or old[0].get('session_date')!=day.isoformat() or old[0].get('security_id')!=str(index['security_id'])):
        body={'securityId':str(index['security_id']),'exchangeSegment':'IDX_I','instrument':'INDEX','interval':'1','oi':False,
              'fromDate':(start-timedelta(days=21)).strftime('%Y-%m-%d %H:%M:%S'),'toDate':start.strftime('%Y-%m-%d %H:%M:%S')}
        try:
            raw=await provider.post('charts/intraday',body);candle_count(raw)
            record={'symbol':symbol,'security_id':str(index['security_id']),'session_date':day.isoformat(),'received_at':datetime.now(timezone.utc).isoformat(),'request':body,'raw':raw}
            await asyncio.to_thread(replace,store,key,lambda previous:record)
        except ProviderError as exc:report['warmup_error']=str(exc)
    future=board.instruments.get(symbol+'_future')
    if not future:return {**report,'futures_vwap':{'state':'MASTER_IDENTITY_UNAVAILABLE','value':None}}
    key='research/future/'+symbol;old=await asyncio.to_thread(store.document,key)
    cached=old[0];at=stamp((cached or {}).get('received_at'))
    if cached and cached.get('security_id')==str(future['security_id']) and cached.get('expiry')==future['expiry'] and at and 0<=(clock-at).total_seconds()<55:
        return {**report,'futures_vwap':future_vwap(cached,clock)}
    end=min(local.replace(second=0,microsecond=0),start.replace(hour=15,minute=30))
    if day.weekday()>4 or end<=start:return {**report,'futures_vwap':{'state':'OUTSIDE_REGULAR_SESSION','value':None}}
    body={'securityId':str(future['security_id']),'exchangeSegment':'NSE_FNO','instrument':'FUTIDX','interval':'1','oi':True,
          'fromDate':start.strftime('%Y-%m-%d %H:%M:%S'),'toDate':end.strftime('%Y-%m-%d %H:%M:%S')}
    try:
        raw=await provider.post('charts/intraday',body);candle_count(raw)
        record={'symbol':symbol,'security_id':str(future['security_id']),'expiry':future['expiry'],'session_date':day.isoformat(),
                'received_at':datetime.now(timezone.utc).isoformat(),'request':body,'raw':raw}
        await asyncio.to_thread(replace,store,key,lambda previous:record)
        return {**report,'futures_vwap':future_vwap(record,datetime.now(timezone.utc))}
    except ProviderError as exc:return {**report,'futures_vwap':{'state':'PROVIDER_ERROR','value':None,'reason':str(exc)}}

def future_vwap(record,clock):
    view=session_view(record,clock);rows=view['candles'];volume=sum(r['volume'] for r in rows)
    valid=not view['missing_minutes'] and not view['conflicting_minutes'] and volume>0 and view['freshness']['state']=='FRESH'
    return {'state':'FRESH_CANDLE_APPROXIMATION' if valid else 'INCOMPLETE_OR_STALE','security_id':record['security_id'],
            'expiry':record['expiry'],'value':sum((r['high']+r['low']+r['close'])/3*r['volume'] for r in rows)/volume if valid else None,
            'received_at':record['received_at'],'missing_minutes':view['missing_minutes'],
            'basis':'Current exact futures contract; HLC3 × candle volume / volume. Candle approximation, not tick VWAP.'}
