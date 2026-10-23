"""Bounded, read-only live evidence validation. Never repairs or invents data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,date
from math import isfinite
from runtime import IST
from data_calendar import trading_state
from session_chart import session_view
from provider import ProviderError

VERSION='live-data-1.0'

def timestamp(value):
    try:
        parsed=datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError,ValueError):return None

def age(value,clock):
    at=timestamp(value)
    return (clock-at).total_seconds() if at else None

def number(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and isfinite(value)

def check(name,state,detail,**evidence):
    return {'check':name,'state':state,'detail':detail,**evidence}

def candles(record,symbol,identity,clock):
    if not record:return check(symbol+' candles','BLOCKED','No current-session provider snapshot')
    if (not identity or record.get('symbol')!=symbol or str(record.get('security_id'))!=str(identity.get('security_id'))
            or record.get('session_date')!=clock.astimezone(IST).date().isoformat()):
        return check(symbol+' candles','BLOCKED','Snapshot session / exact security identity mismatch')
    received_age=age(record.get('received_at'),clock)
    if received_age is None or received_age<0:
        return check(symbol+' candles','BLOCKED','Missing, naive or future receipt timestamp')
    try:
        raw=record['raw'];view=session_view(record,clock)
        if any(not number(v) or v<=0 for key in ('open','high','low','close') for v in raw[key]):
            raise ValueError('Nonpositive price')
    except (ProviderError,ValueError,TypeError,KeyError,OverflowError,OSError):
        return check(symbol+' candles','BLOCKED','Invalid OHLCV arrays, prices, volume or timestamps')
    stamps=raw['timestamp'];duplicates=len(stamps)-len(set(stamps))
    reversed_times=sum(a>b for a,b in zip(stamps,stamps[1:]))
    calendar=view['calendar']['state']=='REGULAR'
    valid=(calendar and not reversed_times and view['expected_minutes']>0 and not view['missing_minutes'] and not view['conflicting_minutes']
           and view['freshness']['state']=='FRESH' and received_age<=90)
    return check(symbol+' candles','PASS' if valid and not duplicates else 'WARN' if valid else 'BLOCKED',
                 'Closed provider minute grid; duplicate rows deduplicated, conflicts excluded. No synthetic fill.',
                 security_id=record['security_id'],session_date=record['session_date'],
                 received_at=record['received_at'],receipt_age_seconds=round(received_age,2),
                 expected_minutes=view['expected_minutes'],observed_minutes=len(view['candles']),
                 missing_minutes=view['missing_minutes'],missing_timestamps=view['missing_timestamps'][:10],
                 conflicting_minutes=view['conflicting_minutes'],duplicate_rows=duplicates,out_of_order_rows=reversed_times,
                 excluded_rows=view['excluded_rows'],freshness=view['freshness']['state'],calendar=view['calendar']['state'])

def chain(record,symbol,clock):
    if not record:return [check(symbol+' chain','BLOCKED','No current chain snapshot')]
    meta=record.get('metadata',{});raw=record.get('raw',{})
    meta=meta if isinstance(meta,dict) else {};raw=raw if isinstance(raw,dict) else {}
    data=raw.get('data');oc=data.get('oc') if isinstance(data,dict) else None
    try:valid=meta.get('index')==symbol and date.fromisoformat(meta.get('expiry'))>=clock.astimezone(IST).date()
    except (TypeError,ValueError):valid=False
    received_age=age(meta.get('fetched_at'),clock)
    errors=0;crossed=0;zero_sided=0;contracts=0
    if not isinstance(oc,dict) or not oc:valid=False
    else:
        for strike,pair in oc.items():
            try:
                if not isfinite(float(strike)) or float(strike)<=0:raise ValueError()
            except (ValueError,TypeError):errors+=1
            for side in ('ce','pe'):
                value=pair.get(side) if isinstance(pair,dict) else None
                if not isinstance(value,dict):errors+=1;continue
                contracts+=1
                if any(not number(value.get(k)) or value[k]<0 for k in ('last_price','oi','volume','top_bid_price','top_ask_price')):errors+=1;continue
                bid,ask=value['top_bid_price'],value['top_ask_price']
                if bid and ask and bid>ask:crossed+=1
                if not bid or not ask:zero_sided+=1
    structurally_valid=valid and not errors and not crossed and received_age is not None and 0<=received_age<=90
    # A fresh server receipt cannot certify exchange quote freshness.
    exchange_age=age(meta.get('exchange_at'),clock)
    exchange=timestamp(meta.get('exchange_at'));received=timestamp(meta.get('fetched_at'))
    time_valid=(exchange_age is not None and 0<=exchange_age<=10 and received is not None and exchange<=received<=clock)
    return [check(symbol+' chain structure','PASS' if structurally_valid else 'BLOCKED',
                  'All returned strikes; zero-sided contracts remain untradeable; identity is checked separately per selected contract.',
                  expiry=meta.get('expiry'),contracts=contracts,invalid_fields=errors,crossed_quotes=crossed,
                  zero_sided_contracts=zero_sided,received_at=meta.get('fetched_at'),receipt_age_seconds=received_age),
            check(symbol+' chain exchange time','PASS' if structurally_valid and time_valid else 'BLOCKED',
                  'Exchange quote time required. Server receipt / last-trade time is not substituted.',exchange_at=meta.get('exchange_at'))]

def report(store,clock=None,symbol=None):
    clock=clock or datetime.now(timezone.utc)
    symbols=[symbol] if symbol else ['nifty','banknifty','india_vix']
    keys=['control/worker','control/data-system','system/subscription-plan']
    keys+=['system/recorder/'+f for f in ('live','live-extra','depth20','depth200')]
    keys+=['research/session/'+s for s in symbols]
    keys+=['research/future/'+s for s in symbols if s!='india_vix']
    keys+=['research/current-chain/'+s for s in symbols if s!='india_vix']
    with ThreadPoolExecutor(max_workers=8) as pool:documents=dict(zip(keys,pool.map(lambda k:store.document(k)[0] or {},keys)))
    documents={k:v if isinstance(v,dict) else {'invalid_document':True} for k,v in documents.items()}
    worker=documents['control/worker'];plan=documents['system/subscription-plan']
    from build_identity import runtime_build
    worker_age=age(worker.get('heartbeat'),clock)
    worker_ok=number(worker.get('until')) and worker['until']>clock.timestamp() and worker_age is not None and 0<=worker_age<=90 and worker.get('build_id')==runtime_build()
    checks=[check('Collector release / heartbeat','PASS' if worker_ok else 'BLOCKED','Running build must match the serving app and heartbeat must be recent.',heartbeat_age_seconds=worker_age)]
    checks.append(check('Automatic recording enabled','PASS' if documents['control/data-system'].get('enabled') is True else 'BLOCKED','Paused recording does not establish current data readiness.'))
    is_open=trading_state(clock)=='OPEN'
    checks.append(check('NSE regular session','PASS' if is_open else 'CLOSED','Only documented NSE regular sessions certify this validator. Other exchanges require their own calendars.'))
    scopes=plan.get('scopes');scopes=scopes if isinstance(scopes,dict) else {}
    scopes={k:v if isinstance(v,list) else [] for k,v in scopes.items()}
    for family in ('live','live-extra','depth20','depth200'):
        record=documents['system/recorder/'+family];scope=scopes.get(family,[])
        receipt_age=age(record.get('last_durable_received_at'),clock);heartbeat_age=age(record.get('heartbeat'),clock)
        valid=bool(scope) and record.get('state')=='RECORDING' and receipt_age is not None and 0<=receipt_age<=30 and heartbeat_age is not None and 0<=heartbeat_age<=90
        valid=valid and record.get('subscription_revision')==plan.get('revision') and record.get('subscribed')==len(scope) and record.get('missing_since_connection')==0 and record.get('decode_errors')==0
        checks.append(check(family+' durable feed','PASS' if valid and worker_ok else 'BLOCKED' if is_open else 'CLOSED',
                            'Receipt and decoded subscription coverage only; lossless ticks / depth or per-instrument exchange freshness are not certified.',
                            receipt_age_seconds=receipt_age,subscribed=len(scope),missing=record.get('missing_since_connection'),decode_errors=record.get('decode_errors')))
    for s in symbols:
        identity=next((i for group in scopes.values() for i in group if isinstance(i,dict) and i.get('key')==s),None)
        checks.append(candles(documents['research/session/'+s],s,identity,clock))
        if s!='india_vix':
            future=documents['research/future/'+s]
            future_identity=next((i for group in scopes.values() for i in group if isinstance(i,dict) and i.get('key')==s+'_future'),None)
            future_check=candles(future,s,future_identity,clock);future_check['check']=s+' exact future candles'
            if not future_identity or future.get('expiry')!=future_identity.get('expiry') or str(future.get('expiry') or '')<clock.astimezone(IST).date().isoformat():
                future_check.update(state='BLOCKED',detail='Exact current futures expiry / identity not verified')
            checks.append(future_check)
            checks.extend(chain(documents['research/current-chain/'+s],s,clock))
    blocked=sum(c['state']=='BLOCKED' for c in checks)
    return {'version':VERSION,'checked_at':clock.isoformat(),'state':'BLOCKED' if blocked else 'CLOSED' if not is_open else 'PASS_WITH_WARNINGS' if any(c['state']=='WARN' for c in checks) else 'PASS',
            'blocked_checks':blocked,'checks':checks,'data_eligible':is_open and not blocked,
            'scope':'Bounded current-session snapshots and configured recorder scopes; not full historical/all-market coverage or a trading recommendation.'}

def gate(setups,validation):
    if validation['data_eligible']:return setups
    return [{**s,'state':'DATA_UNAVAILABLE','missing':list(dict.fromkeys(s['missing']+['DATA_VALIDATOR_BLOCKED'])),
             'reason_codes':list(dict.fromkeys(s['reason_codes']+['DATA_VALIDATOR_BLOCKED']))} for s in setups]
