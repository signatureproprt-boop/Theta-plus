"""Observable coverage from retained raw history, without invented candles."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from provider import ProviderError
from runtime import IST
from data_calendar import session
from dhan_data import candle_count, option_count


def coverage_report(store,start,end,include_rows=False):
    if not start<end or (end-start).days>31:raise ValueError('Use a range of 1–31 days')
    groups={};invalid=[]
    for saved in store.rows('dhan_history',500):
        record=saved['data'];meta=record.get('metadata',{});kind=meta.get('kind')
        if kind not in ('index','expired_option'):continue
        if meta.get('index') not in ('nifty','banknifty','india_vix'):continue
        # Responses can contain boundary spillover; inspect actual timestamps.
        raw=record.get('raw',{})
        row=raw if kind=='index' else (raw.get('data') or {}).get('ce' if meta.get('side')=='CALL' else 'pe')
        if row is None:continue
        try:
            if kind=='index':candle_count(raw)
            else:option_count(raw,meta['side'])
        except (ProviderError,KeyError,TypeError) as exc:
            invalid.append({'record_id':saved['id'],'reason':str(exc)});continue
        identity={k:meta.get(k) for k in ('kind','index','security_id','side','strike','expiry_flag','expiry_code')}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        group=groups.setdefault(key,{'id':key,**identity,'candles':{},'source_record_ids':set(),'overlap_rows':0,'conflicts':set(),'off_session':set()})
        for i,stamp in enumerate(row['timestamp']):
            at=datetime.fromtimestamp(stamp,timezone.utc).astimezone(IST)
            if not start<=at.date()<end:continue
            group['source_record_ids'].add(saved['id'])
            if session(at.date())['state']=='CLOSED' or not '09:15'<=at.strftime('%H:%M')<'15:30':group['off_session'].add(int(stamp));continue
            values={k:v[i] for k,v in row.items() if isinstance(v,list) and len(v)==len(row['timestamp'])}
            identity_key=str(int(stamp))+(':'+str(float(values['strike'])) if kind=='expired_option' else '')
            old=group['candles'].get(identity_key)
            if old is not None:
                group['overlap_rows']+=1
                if old!=values:group['conflicts'].add(identity_key)
            else:group['candles'][identity_key]=values
    days=[];day=start
    while day<end:
        days.append(day)
        day+=timedelta(days=1)
    results=[]
    for group in groups.values():
        if not group['source_record_ids']:continue
        values=[v for k,v in sorted(group['candles'].items()) if k not in group['conflicts']]
        stamps=defaultdict(set)
        for v in values:
            at=datetime.fromtimestamp(v['timestamp'],timezone.utc).astimezone(IST)
            stamps[at.date()].add(at.strftime('%H:%M'))
        by_minute=defaultdict(set)
        for v in values:by_minute[int(v['timestamp'])].add(v.get('strike'))
        ambiguous=sum(len(strikes)>1 for strikes in by_minute.values())
        sessions=[]
        for day in days:
            calendar=session(day)
            if calendar['state']=='CLOSED':continue
            if calendar['state']!='REGULAR':
                sessions.append({'date':day.isoformat(),'minutes':len(stamps[day]),'missing':None,'expected_minutes':None,'calendar_state':calendar['state']})
                continue
            expected={(datetime(2000,1,1,9,15)+timedelta(minutes=i)).strftime('%H:%M') for i in range(375)}
            missing=sorted(expected-stamps[day])
            sessions.append({'date':day.isoformat(),'minutes':len(stamps[day]),'missing':len(missing),'missing_ist':missing[:20],'expected_minutes':375,'calendar_state':calendar['state']})
        result={k:v for k,v in group.items() if k not in ('candles','conflicts','source_record_ids','off_session')}
        result.update(rows=len(values),source_record_ids=sorted(group['source_record_ids']),conflicting_rows=len(group['conflicts']),
                      off_session_rows_excluded=len(group['off_session']),sessions=sessions,
                      ambiguous_minutes=ambiguous,weekday_grid_complete=not ambiguous and bool(sessions) and all(s['missing']==0 for s in sessions) and not group['conflicts'],
                      fixed_contract_backtest_ready=False if group['kind']=='expired_option' else None)
        if include_rows:result['candles']=values
        results.append(result)
    return {'from':start.isoformat(),'to_exclusive':end.isoformat(),'groups':sorted(results,key=lambda g:(g['kind'],g['index'],g['side'] or '')),
            'invalid_records':invalid,'calendar_validation':'Documented NSE 2026 regular sessions; other years and special-session times remain unverified.',
            'source_records_inspected_limit':500,
            'provenance':'Raw records retained. Overlaps deduplicated; conflicting rows excluded. No missing candles filled.',
            'options':'ATM-relative strikes can change. Exact contract expiry and historical bid/ask are not established.'}
