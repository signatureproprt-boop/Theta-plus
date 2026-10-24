"""Whole-master history scope and earlier cash/index daily backfill.

Plan scope and request outcomes are distinct from verified instrument/day data.
The existing five-year jobs, saved rows and checkpoints remain unchanged.
"""
from datetime import date,datetime,timezone
from collection import CollectionPlan,digest,replace
from catalogue_backfill import additions
from instrument_directory import InstrumentDirectory

KEY='system/whole-market'
EARLIER='system/earlier-daily'
CASH_SEGMENTS=['IDX_I','NSE_EQ','BSE_EQ']


def descriptor(collector,jid,role):
    job=collector.get_job(jid);cfg=job['config']
    return {'role':role,'job_id':jid,'state':job['state'],'from':cfg['start'],'to_exclusive':cfg['end'],
        'families':cfg['families'],'intervals':cfg['intervals'],'segments':cfg.get('segments',[]),
        'symbols':cfg.get('symbols',[]),'work_items_assessed':job['items_assessed'],
        'rows_saved_across_attempts':job['rows_saved'],'attempt_outcomes':job['counts'],
        'catalogue_sha256':job['catalogue_sha256'],'manually_paused':bool(job.get('system_user_paused'))}


def earlier_daily(collector,cat,root):
    """Request daily history before the existing five-year cash/index boundary."""
    store=collector.store;state=store.document(EARLIER)[0] or {}
    if root.get('system_user_paused'):return {**state,'state':'SOURCE_MANUALLY_PAUSED'}
    end=date.fromisoformat(root['config']['start'])
    if end<=date(1900,1,1):return {**state,'state':'EARLIER_RANGE_ALREADY_REQUESTED'}
    if state.get('catalogue_sha256')==cat['sha256']:return state
    if any(collector.get_job(j).get('system_user_paused') for j in state.get('job_ids',[])):
        return {**state,'state':'EARLIER_MANUALLY_PAUSED'}
    pages=cat['pages'];previous=state.get('catalogue_sha256')
    if previous:
        old=store.document('catalogue/'+previous+'/manifest')[0]
        if not old or old.get('sha256')!=previous:raise ValueError('Earlier-daily comparison master missing; original plans retained')
        pages,_=additions(collector,old,cat,False)
    pages=[{**p,'family':'daily'} for p in pages if any(p['group'].startswith(seg+'-') for seg in CASH_SEGMENTS)]
    pages.sort(key=lambda p:(next(i for i,s in enumerate(CASH_SEGMENTS) if p['group'].startswith(s+'-')),p['key']))
    jobs=list(state.get('job_ids',[]));jid=None
    if pages:
        jid=digest(['whole-market-earlier-daily-v1',root['id'],cat['sha256'],previous,end.isoformat()])[:32]
        plan=CollectionPlan(start='1900-01-01',end=end,families=['daily'],intervals=[1],segments=CASH_SEGMENTS,
            continue_rejected_requests=True,quarantine_invalid_candles=True)
        collector.create(plan,job_id=jid,catalogue=cat)
        def prepare(job):
            if job.get('earlier_daily_scope'):return job
            if job['state']!='paused' or job['items_assessed'] or job.get('current'):raise ValueError('Earlier-daily job already advanced before pinning scope')
            return {**job,'pages':pages,'system_role':'all_daily_earlier','earlier_daily_scope':True,
                'message':'Earlier cash/index daily history queued. 1900 is the requested lower boundary, not a claim of provider availability.'}
        job=replace(store,'jobs/'+jid,prepare)
        if job['state']=='paused' and not job.get('system_user_paused'):collector.control(jid,'resume')
        if jid not in jobs:jobs.append(jid)
    state={'catalogue_sha256':cat['sha256'],'previous_sha256':previous,'job_ids':jobs,'latest_job_id':jid or state.get('latest_job_id'),
        'from':'1900-01-01','to_exclusive':end.isoformat(),'segments':CASH_SEGMENTS,
        'scope':'Earlier daily cash/index requests, including additions from later masters. The actual first available source date is determined by returned candles; rejected/empty ranges stay unresolved.'}
    replace(store,EARLIER,lambda _:state)
    return state


def refresh(collector,clock=None):
    clock=clock or datetime.now(timezone.utc);store=collector.store;cat=collector.catalogue()
    roots=(store.document('system/backfill')[0] or {}).get('jobs',{})
    if not cat or not roots:return {'state':'WHOLE_MARKET_NOT_CONFIGURED','coverage_verified':False}
    directory=getattr(collector,'_whole_market_directory',None)
    if directory is None:
        directory=InstrumentDirectory(collector);collector._whole_market_directory=directory
    segments=directory.market_scope()
    plans=[];issues=[]
    def append(jid,role):
        try:plans.append(descriptor(collector,jid,role))
        except Exception as exc:issues.append({'role':role,'job_id':jid,'state':'JOB_RECORD_UNAVAILABLE','error_type':type(exc).__name__})
    for family,jid in roots.items():append(jid,'backfill_'+family)
    additions_state=(store.document('system/backfill-catalogue')[0] or {}).get('families',{})
    for family,slot in additions_state.items():
        if slot.get('job_id'):append(slot['job_id'],'catalogue_additions_'+family)
    maintenance=(store.document('system/maintenance')[0] or {}).get('history',{})
    for role,slot in maintenance.items():
        if role.startswith('all_') and slot.get('job_id'):append(slot['job_id'],role)
    equity=(store.document('system/equity-catchup')[0] or {}).get('families',{})
    for family,slot in equity.items():
        ids=list(slot.get('job_ids',[]))
        if slot.get('latest_job_id') and slot['latest_job_id'] not in ids:ids.append(slot['latest_job_id'])
        for jid in ids:append(jid,'equity_recent_'+family)
    earlier={}
    if roots.get('daily'):
        try:earlier=earlier_daily(collector,cat,collector.get_job(roots['daily']))
        except Exception as exc:
            earlier={'state':'EARLIER_DAILY_REVIEW_REQUIRED'}
            issues.append({'role':'earlier_daily_cash_indices','state':'NOT_VERIFIED','error_type':type(exc).__name__})
        for jid in earlier.get('job_ids',[]):append(jid,'earlier_daily_cash_indices')
    report={'checked_at':clock.isoformat(),'state':'RUNNING' if any(p['state']=='running' for p in plans) else 'REVIEW_OUTCOMES',
        **segments,'plans':plans,'read_issues':issues,'earlier_daily':earlier,'coverage_verified':False,
        'scope':'Whole mapped Dhan catalogue: historical plans plus catalogue additions and recent catch-up. No symbol filter on full-market roots. Cash/index daily requests extend before the five-year roots. Minute/rolling availability remains bounded by the provider horizon and exact contracts.',
        'note':'Catalogue identities, work items and rows saved across attempts are not a unique-coverage percentage. Inspect instrument/day coverage for received and missing rows; unresolved API segments remain excluded.'}
    replace(store,KEY,lambda _:report)
    return report
