"""Read-only Dhan field inventory; never stores broker credentials or raw quotes."""
from datetime import date,timedelta,datetime,timezone
from engine import IST

BAR_FIELDS=('timestamp','open','high','low','close','volume')
OPTION_FIELDS=('open','high','low','close','iv','volume','strike','oi','spot')

def _shape(obj,depth=0):
    if depth>5:return []
    if isinstance(obj,dict):
        fields=[]
        for key,value in obj.items():
            if str(key).startswith('_'):continue
            fields.append(str(key))
            if isinstance(value,(dict,list)):
                fields.extend(str(key)+'.'+child for child in _shape(value,depth+1))
        return fields[:150]
    if isinstance(obj,list) and obj:return _shape(obj[0],depth+1)
    return []

def _array_health(raw,day):
    lengths={key:len(raw[key]) for key in BAR_FIELDS if isinstance(raw.get(key),list)}
    complete=len(lengths)==len(BAR_FIELDS) and len(set(lengths.values()))==1
    stamps=raw.get('timestamp',[]) if isinstance(raw.get('timestamp'),list) else []
    observed=[]
    for stamp in stamps:
        observed.append(datetime.fromtimestamp(stamp,timezone.utc).astimezone(IST).strftime('%Y-%m-%d %H:%M'))
    expected={f'{day.isoformat()} {(datetime(2000,1,1,9,15)+timedelta(minutes=i)):%H:%M}' for i in range(375)}
    missing=sorted(expected-set(observed))
    return {'bars':len(stamps),'schema_valid':complete,
            'regular_session_complete':complete and not missing and len(observed)==375,
            'missing_minutes':len(missing),'missing_times_ist':[s[-5:] for s in missing[:12]],
            'duplicate_minutes':len(observed)-len(set(observed)),
            'off_session_minutes':len(set(observed)-expected),
            'start':datetime.fromtimestamp(min(stamps),timezone.utc).isoformat() if stamps else None,
            'end':datetime.fromtimestamp(max(stamps),timezone.utc).isoformat() if stamps else None}

def _option_rows(obj):
    if isinstance(obj,dict):
        arrays={k:v for k,v in obj.items() if k in ('open','high','low','close') and isinstance(v,list)}
        if len(arrays)==4 and len({len(v) for v in arrays.values()})==1:
            return len(arrays['open'])
        return max((_option_rows(v) for v in obj.values() if isinstance(v,(dict,list))),default=0)
    if isinstance(obj,list):
        return max((_option_rows(v) for v in obj[:10] if isinstance(v,(dict,list))),default=0)
    return 0

async def audit_historical(provider,day:date):
    today=datetime.now(IST).date()
    if not today-timedelta(days=90)<=day<today:raise ValueError('Choose a past day within 90 days')
    start=day.strftime('%Y-%m-%d')
    end=(day+timedelta(days=1)).strftime('%Y-%m-%d')
    result={'date':start,'checked_at':datetime.now(timezone.utc).isoformat(),
            'index':None,'expired_call':None,'expired_put':None,
            'fixed_strike_backtest_ready':False,
            'notes':['Index history and ATM-relative option candles do not include historical option bid/ask depth.',
                     'ATM-relative rolling strikes may change. No fixed-strike trade fills are inferred.']}
    from provider import ProviderError
    start_time=start+' 09:15:00';end_time=end+' 00:00:00'
    try:
        raw=await provider.post('charts/intraday',{'securityId':'13','exchangeSegment':'IDX_I',
            'instrument':'INDEX','interval':'1','oi':False,'fromDate':start_time,'toDate':end_time})
        health=_array_health(raw,day)
        result['index']={'status':'AVAILABLE' if health['regular_session_complete'] else 'PARTIAL' if health['bars'] and health['schema_valid'] else 'EMPTY_OR_INVALID',**health,'fields':_shape(raw)}
    except (ProviderError,ValueError,TypeError,OverflowError):
        result['index']={'status':'UNAVAILABLE','bars':0,'schema_valid':False}
    for side,name in (('CALL','expired_call'),('PUT','expired_put')):
        try:
            raw=await provider.post('charts/rollingoption',{
                'exchangeSegment':'NSE_FNO','interval':'1','securityId':13,'instrument':'OPTIDX',
                'expiryFlag':'MONTH','expiryCode':1,'strike':'ATM','drvOptionType':side,
                'requiredData':list(OPTION_FIELDS),'fromDate':start,'toDate':end})
            fields=_shape(raw)
            rows=_option_rows(raw)
            result[name]={'status':'ROWS_PRESENT' if rows else 'FIELDS_ONLY' if fields else 'EMPTY',
                          'observed_rows':rows,'relative_strike':True,'fields':fields,
                          'top_level_keys':sorted(raw.keys()),'bid_ask_present':
                          any('bid' in f.lower() or 'ask' in f.lower() for f in fields)}
        except (ProviderError,ValueError,TypeError):
            result[name]={'status':'UNAVAILABLE','fields':[],'bid_ask_present':False}
    return result
