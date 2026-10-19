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
