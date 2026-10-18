"""Bounded, read-only Dhan data acquisition with preserved source payloads."""
from datetime import date, datetime, timedelta, timezone
from collections import Counter
import time
from engine import IST
from provider import ProviderError

INDEX_IDS={'nifty':13}  # Other IDs are resolved from the current Dhan instrument master.
FIELDS=('timestamp','open','high','low','close','volume')
OPTION_FIELDS=('open','high','low','close','iv','volume','strike','oi','spot')

def candle_count(raw):
    if not isinstance(raw,dict) or not all(isinstance(raw.get(k),list) for k in FIELDS):
        raise ProviderError('Historical response has no complete OHLCV arrays')
    if len({len(raw[k]) for k in FIELDS})!=1:
        raise ProviderError('Historical OHLCV arrays have different lengths')
    return len(raw['timestamp'])

def option_count(raw,side):
    row=(raw.get('data') or {}).get('ce' if side=='CALL' else 'pe') if isinstance(raw,dict) else None
    if row is None:return 0
    if not isinstance(row,dict) or not isinstance(row.get('timestamp'),list):
        raise ProviderError('Expired option response has no timestamp array')
    lengths=[len(row[k]) for k in ('timestamp',)+OPTION_FIELDS if isinstance(row.get(k),list)]
    if len(set(lengths))!=1:raise ProviderError('Expired option arrays have different lengths')
    return len(row['timestamp'])

async def fetch_history(provider,store,*,kind,index,start,end,strike='ATM',side='CALL',expiry_flag='MONTH',expiry_code=1,instrument=None):
    """Fetch at most 30 calendar days; persist raw response and its provenance."""
    today=datetime.now(IST).date()
    if not start<end<=today+timedelta(days=1) or (end-start).days>30 or start<today-timedelta(days=5*366):
        raise ValueError('Choose a past range of at most 30 days within the last five years')
    if index not in ('nifty','banknifty','india_vix'):raise ValueError('Choose a supported index')
    expected_segment,expected_type=('NSE_FNO','FUTIDX') if kind=='future' else ('IDX_I','INDEX')
    if not instrument or instrument.get('segment')!=expected_segment or instrument.get('instrument')!=expected_type:
        raise ProviderError('Exact security ID is not available in current Dhan instrument master')
    sid=int(instrument['security_id'])
    if kind in ('index','future'):
        body={'securityId':str(sid),'exchangeSegment':expected_segment,'instrument':expected_type,'interval':'1','oi':kind=='future',
              'fromDate':start.isoformat()+' 09:15:00','toDate':end.isoformat()+' 00:00:00'}
        endpoint='charts/intraday'
    elif kind=='expired_option':
        if index=='india_vix':raise ValueError('VIX has no index option history here')
        if strike not in (['ATM']+[f'ATM{sign}{n}' for sign in ('+','-') for n in range(1,11)]):
            raise ValueError('Choose ATM or ATM ± 1–10')
        if side not in ('CALL','PUT') or expiry_flag not in ('WEEK','MONTH') or not 1<=expiry_code<=5:
            raise ValueError('Invalid option selection')
        body={'exchangeSegment':'NSE_FNO','interval':'1','securityId':sid,'instrument':'OPTIDX',
              'expiryFlag':expiry_flag,'expiryCode':expiry_code,'strike':strike,'drvOptionType':side,
              'requiredData':list(OPTION_FIELDS),'fromDate':start.isoformat(),'toDate':end.isoformat()}
        endpoint='charts/rollingoption'
    else:raise ValueError('Unsupported historical data type')
    raw=await provider.post(endpoint,body)
    count=candle_count(raw) if kind in ('index','future') else option_count(raw,side)
    metadata={'provider':'DhanHQ','endpoint':endpoint,'kind':kind,'index':index,'from':start.isoformat(),
              'security_id':str(sid),
              'to_exclusive':end.isoformat(),'strike':strike if kind=='expired_option' else None,
              'side':side if kind=='expired_option' else None,'expiry_flag':expiry_flag if kind=='expired_option' else None,
              'expiry_code':expiry_code if kind=='expired_option' else None,'rows':count,
              'fetched_at':datetime.now(timezone.utc).isoformat(),'relative_strike':kind=='expired_option',
              'historical_bid_ask':False,'contract_expiry':instrument.get('expiry') if kind=='future' else None}
    # An empty response is recorded too, so it cannot silently appear as successful data.
    record_id=store.add('dhan_history',{'metadata':metadata,'raw':raw})
    return {'id':record_id,**metadata,'quality':verify_record({'metadata':metadata,'raw':raw})}

async def fetch_chain(provider,store,index,instrument):
    if index not in ('nifty','banknifty'):raise ValueError('Choose NIFTY or BANKNIFTY')
    if not instrument or instrument.get('segment')!='IDX_I':
        raise ProviderError('Exact index security ID is not available in Dhan instrument master')
    sid=int(instrument['security_id'])
    cache=getattr(provider,'_theta_expiry_cache',{})
    cached=cache.get(index)
    if cached and time.monotonic()-cached[1]<300 and cached[0]>=datetime.now(IST).date().isoformat():
        expiry=cached[0]
    else:
        expiries=(await provider.post('optionchain/expirylist',{'UnderlyingScrip':sid,'UnderlyingSeg':'IDX_I'})).get('data',[])
        future=sorted(e for e in expiries if isinstance(e,str) and e>=datetime.now(IST).date().isoformat())
        if not future:raise ProviderError('No future option expiry returned by Dhan')
        expiry=future[0]
        cache[index]=(expiry,time.monotonic());provider._theta_expiry_cache=cache
    raw=await provider.post('optionchain',{'UnderlyingScrip':sid,'UnderlyingSeg':'IDX_I','Expiry':expiry})
    oc=(raw.get('data') or {}).get('oc')
    if not isinstance(oc,dict):raise ProviderError('Option chain response has no strike map')
    metadata={'provider':'DhanHQ','kind':'live_option_chain','index':index,'expiry':expiry,
              'strikes':len(oc),'fetched_at':datetime.now(timezone.utc).isoformat(),
              'time_basis':'server receipt; no exchange quote timestamp in chain response'}
    record_id=store.add('dhan_chain',{'metadata':metadata,'raw':raw})
    return {'id':record_id,**metadata}

def verify_record(record):
    """Report only observable quality of one stored Dhan response."""
    meta=record['metadata'];raw=record['raw'];kind=meta['kind']
    if kind=='live_option_chain':
        oc=(raw.get('data') or {}).get('oc') or {}
        return {'kind':kind,'strikes':len(oc),'received_at':meta['fetched_at'],
                'note':'Server receipt time, not exchange quote time. No historical OHLC.'}
    row=raw if kind in ('index','future') else (raw.get('data') or {}).get('ce' if meta['side']=='CALL' else 'pe')
    if not isinstance(row,dict) or not isinstance(row.get('timestamp'),list):
        return {'kind':kind,'rows':0,'status':'EMPTY','sessions':[]}
    stamps=row['timestamp'];parsed=[]
    for stamp in stamps:
        parsed.append(datetime.fromtimestamp(stamp,timezone.utc).astimezone(IST))
    counts=Counter(t.strftime('%Y-%m-%d %H:%M') for t in parsed)
    by_day={}
    for t in parsed:by_day.setdefault(t.date(),set()).add(t.strftime('%H:%M'))
    sessions=[]
    for day,times in sorted(by_day.items()):
        expected={(datetime(2000,1,1,9,15)+timedelta(minutes=i)).strftime('%H:%M') for i in range(375)}
        missing=sorted(expected-times)
        sessions.append({'date':day.isoformat(),'minute_rows':sum(t.date()==day for t in parsed),
                         'missing_count':len(missing),'missing_ist':missing[:20],
                         'off_session_count':len(times-expected)})
    result={'kind':kind,'rows':len(stamps),'status':'ROWS_PRESENT' if stamps else 'EMPTY',
            'first_ist':min(parsed).isoformat() if parsed else None,
            'last_ist':max(parsed).isoformat() if parsed else None,
            'duplicate_minutes':sum(n-1 for n in counts.values()),'sessions':sessions,
            'note':'Observed days only. Holidays, entirely absent days and special sessions need an exchange calendar.'}
    if kind=='expired_option':
        strikes=row.get('strike',[])
        result.update({'distinct_actual_strikes':len(set(strikes)) if isinstance(strikes,list) else None,
                       'exact_expiry_verified':False,'historical_bid_ask':False})
    return result
