"""Bounded late-publication recovery of exact core NSE index sessions.

Original results stay immutable. Completion is recomputed from retained candles,
not from a job finishing or a retry returning any rows. No synthetic rows.
"""
import math
from datetime import date,datetime,timedelta,timezone
from collection import digest,replace
from data_calendar import session
from history_grid import requested_grid
from runtime import IST

REPORT='system/session-recovery'
MAX_ATTEMPTS=2
RETRY_SECONDS=7200
IDENTITIES={('NIFTY','13'),('BANKNIFTY','25'),('INDIA VIX','21'),('INDIAVIX','21')}


def session_days(entry):
    task=entry.get('task',{});item=task.get('item',{});result=entry.get('result',{})
    if (result.get('status')!='partial' or result.get('calendar_verified') is not True or
        not (result.get('missing_sessions') or result.get('missing_minutes')) or
        task.get('skip') or item.get('reason') or item.get('exchange')!='NSE' or
        item.get('segment')!='IDX_I' or item.get('instrument')!='INDEX' or
        (item.get('symbol'),item.get('security_id')) not in IDENTITIES):return []
    family=task.get('family');variant=task.get('variant')
    if family=='daily':
        if variant!={}:return []
    elif family=='intraday':
        if not isinstance(variant,dict) or set(variant)!={'interval'} or str(variant['interval'])!='1':return []
    else:return []
    try:
        start=date.fromisoformat(task['from']);end=date.fromisoformat(task['to'])
        if not date(2026,1,1)<=start<end<=datetime.now(IST).date() or (end-start).days>31:return []
        missing=result.get('missing_session_dates')
        # Partial-minute responses with every day represented need a per-day
        # check; inspect the bounded range instead of inventing missing dates.
        if not isinstance(missing,list) or not missing or result.get('missing_minutes'):
            missing=[(start+timedelta(days=n)).isoformat() for n in range((end-start).days) if session(start+timedelta(days=n))['state']=='REGULAR']
        days=[]
        for value in missing:
            day=date.fromisoformat(value)
            if not start<=day<end or session(day)['state']!='REGULAR':return []
            days.append(day.isoformat())
        return sorted(set(days))
    except (KeyError,ValueError,TypeError):return []


def saved_session(collector,task,day):
    item=task['item'];series=digest({'exchange':item['exchange'],'segment':item['segment'],
        'sid':item['security_id'],'instrument':item['instrument'],'family':task['family'],'variant':task['variant']})
    doc=collector.store.document('candles/'+series+'/'+day)[0] or {};values={};invalid=0
    identity=doc.get('instrument',{}) if isinstance(doc,dict) else {}
    matches=(isinstance(doc,dict) and isinstance(identity,dict) and doc.get('series')==series and doc.get('day')==day and
        doc.get('variant')==task['variant'] and all(identity.get(k)==item[k] for k in ('exchange','segment','security_id','instrument')))
    if matches and isinstance(doc.get('candles'),dict):
        for key,row in doc['candles'].items():
            try:
                stamp=row['timestamp'];numbers=[row[k] for k in ('open','high','low','close','volume')]
                if isinstance(stamp,bool) or not isinstance(stamp,(float,int)) or not math.isfinite(stamp) or int(stamp)!=stamp or int(stamp)%60:raise ValueError()
                at=datetime.fromtimestamp(stamp,timezone.utc).astimezone(IST)
                if str(int(stamp))!=key or at.date().isoformat()!=day:raise ValueError()
                if any(isinstance(n,bool) or not isinstance(n,(float,int)) or not math.isfinite(n) for n in numbers):raise ValueError()
                o,h,l,c,v=numbers
                if min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c):raise ValueError()
                values[key]=row
            except (KeyError,ValueError,TypeError,OverflowError,OSError):invalid+=1
    elif doc:invalid+=1
    local={**task,'from':day,'to':(date.fromisoformat(day)+timedelta(days=1)).isoformat()}
    grid=requested_grid(local,{day:values})
    return {'rows':len(values),'invalid_rows':invalid,'missing_minutes':grid.get('missing_minutes'),
        'session_grid_complete':grid.get('session_grid_complete',False) and not invalid}


def tick(collector,clock=None):
    clock=clock or datetime.now(timezone.utc);store=collector.store
    maintenance=store.document('system/maintenance')[0] or {};items=[];queued=0
    for role in ('core_daily','core_intraday'):
        slot=maintenance.get('history',{}).get(role,{})
        if not slot.get('job_id'):continue
        parent=collector.get_job(slot['job_id'])
        if parent.get('state')!='completed' or parent.get('system_user_paused'):continue
        page=collector.results(parent['id'])
        for entry in page['items']:
            for day in session_days(entry):
                task=entry['task'];state=saved_session(collector,task,day)
                item={'source_result':entry['key'],'symbol':task['item']['symbol'],'security_id':task['item']['security_id'],
                    'family':task['family'],'day':day,**state,'state':'SAVED_SESSION_COMPLETE' if state['session_grid_complete'] else 'MISSING_SESSION'}
                if not state['session_grid_complete']:
                    attempts=[]
                    for attempt in range(MAX_ATTEMPTS):
                        jid=digest(['core-session-recovery-v1',entry['key'],day,attempt])[:32]
                        job=store.document('jobs/'+jid)[0]
                        if job:attempts.append(job);continue
                        if attempts:
                            previous=attempts[-1]
                            if previous['state'] not in ('completed','no_work'):break
                            try:elapsed=clock.timestamp()-datetime.fromisoformat(previous['updated_at']).timestamp()
                            except (KeyError,TypeError,ValueError):break
                            if elapsed<RETRY_SECONDS:break
                        if queued>=6:break
                        child=collector.retry(parent['id'],entry['key'],child_id=jid,session_day=day)
                        collector.control(child['id'],'resume');queued+=1
                        job=store.document('jobs/'+jid)[0];attempts.append(job)
                        break
                    item.update(attempts=len(attempts),job_id=attempts[-1]['id'] if attempts else None,
                        state='RECOVERY_QUEUED' if attempts and attempts[-1]['state']=='running' else
                              'UNRESOLVED_AFTER_BOUNDED_RETRIES' if len(attempts)>=MAX_ATTEMPTS and attempts[-1]['state'] in ('completed','no_work') else 'WAITING_OR_REVIEW_REQUIRED')
                    if attempts:item['last_outcomes']=attempts[-1].get('counts',{})
                items.append(item)
    report={'checked_at':clock.isoformat(),'queued_this_scan':queued,'sessions':items,
        'sessions_assessed':len(items),'saved_sessions_complete':sum(i['session_grid_complete'] for i in items),
        'missing_sessions':sum(not i['session_grid_complete'] for i in items),'max_attempts_per_source_session':MAX_ATTEMPTS,
        'retry_delay_seconds':RETRY_SECONDS,'coverage_verified':False,
        'scope':'Exact missing NSE NIFTY/BankNIFTY/VIX daily and 1-minute sessions in current core plans; full archive and all-market recording are not certified.'}
    replace(store,REPORT,lambda _:report)
    return report
