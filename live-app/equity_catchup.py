"""Bounded NSE/BSE catch-up independent of older full-master backfill cursors."""
from datetime import datetime,date,timedelta,timezone
from collection import CollectionPlan,digest,replace
from data_calendar import recent_closed_day

KEY='system/equity-catchup'
SEGMENTS=['IDX_I','NSE_EQ','BSE_EQ','NSE_FNO','BSE_FNO']


def schedule(collector,clock=None):
    clock=clock or datetime.now(timezone.utc);cat=collector.catalogue()
    if not cat:return {'state':'CATALOGUE_UNAVAILABLE','coverage_verified':False}
    # Calendar determines a conservative scheduling target only. It does not
    # certify BSE calendars or exact instrument/session coverage.
    closed=recent_closed_day(clock)
    if closed is None:return {'state':'CALENDAR_UNVERIFIED','coverage_verified':False}
    end=closed+timedelta(days=1);store=collector.store;state=store.document(KEY)[0] or {};families={}
    for family in ('daily','intraday'):
        slot=dict(state.get('families',{}).get(family,{ }));ids=list(slot.get('job_ids',[]))
        jobs=[collector.get_job(jid) for jid in ids]
        if any(j.get('system_user_paused') for j in jobs):
            families[family]={**slot,'state':'MANUALLY_PAUSED'};continue
        finished=[j for j in jobs if j['state'] in ('completed','no_work')]
        if finished:
            slot['completed_jobs']=slot.get('completed_jobs',0)+len(finished)
            slot['last_completed_job_id']=finished[-1]['id']
        jobs=[j for j in jobs if j['state'] not in ('completed','no_work')]
        ids=[j['id'] for j in jobs];slot['job_ids']=ids
        active=len(jobs)
        start=date.fromisoformat(slot['scheduled_to']) if slot.get('scheduled_to') else end-timedelta(days=3)
        if start<end and active<2:
            stop=min(end,start+timedelta(days=3));jid=digest(['equity-recent-v1',family,start.isoformat(),stop.isoformat(),cat['sha256']])[:32]
            plan=CollectionPlan(start=start,end=stop,families=[family],segments=SEGMENTS,intervals=[1],
                continue_rejected_requests=True,quarantine_invalid_candles=True)
            collector.create(plan,job_id=jid,catalogue=cat)
            def pin(job):
                if job.get('equity_catchup_scope'):return job
                if job['state']!='paused' or job['items_assessed']:raise ValueError('Catch-up scope already advanced')
                pages=sorted(job['pages'],key=lambda p:(next(i for i,s in enumerate(SEGMENTS) if p['group'].startswith(s+'-')),p['key']))
                return {**job,'pages':pages,'system_role':'equity_recent_'+family,'equity_catchup_scope':True}
            job=replace(store,'jobs/'+jid,pin)
            if job['state']=='paused' and not job.get('system_user_paused'):collector.control(jid,'resume')
            if jid not in ids:ids.append(jid)
            slot.update(job_ids=ids,scheduled_to=stop.isoformat(),latest_job_id=jid)
            active+=1
        families[family]={**slot,'active_jobs':active,'target_to':end.isoformat(),
            'state':'BACKLOG' if slot.get('scheduled_to','')<end.isoformat() else 'RUNNING' if active else 'REVIEW_OUTCOMES'}
    report={'checked_at':clock.isoformat(),'families':families,'coverage_verified':False,
        'scope':'NSE/BSE cash, indices and F&O; daily and 1-minute requests. At most two unfinished jobs per family. Older ranges keep their original checkpoints; rejected and absent data remain gaps. Scheduling target is not verified exchange coverage.'}
    replace(store,KEY,lambda _:report)
    return report
