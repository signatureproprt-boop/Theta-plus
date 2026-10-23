"""Durable market-data lifecycle: bootstrap, daily catch-up and unattended feeds."""
import asyncio
import csv
import io
from concurrent.futures import ThreadPoolExecutor
import time
import threading
import uuid
from datetime import datetime,timedelta,timezone,date
from collection import CollectionPlan,digest,install_once,replace
from gap_recovery import all_jobs
from market_board import MarketBoard,parse_master
from data_calendar import recent_closed_day,trading_state,session
from runtime import IST
from diagnostics import capture

CONTROL='control/data-system'
SCOPE='system/subscription-plan'

def now():return datetime.now(timezone.utc)

def enabled(store):return bool((store.document(CONTROL)[0] or {}).get('enabled'))

def subscription_plan(board,credential_revision='initial'):
    watch=[{**i,'role':'underlying' if i.get('key') in ('nifty','banknifty') else 'context'}
           for i in board.instruments.values() if i.get('segment') and not i.get('reason')]
    options=[{**i,'key':sid,'segment':'NSE_FNO','instrument':'OPTIDX',
              'label':f"{i['underlying'].upper()} {i['strike']:g} {i['side']}", 'role':'option','identity_verified':True}
             for sid,i in board.options.items() if i['expiry']>=now().astimezone(IST).date().isoformat()]
    options.sort(key=lambda i:(i['expiry'],i['underlying'],i['strike'],i['side']))
    unique={(i['segment'],str(i['security_id'])):i for i in watch+options}
    live=list(unique.values());selected=live[:10000]
    # Full depth has different capacity. Keep exact identities stable in each
    # revision; a scope change is a recorded connection boundary, never continuity.
    futures=[i for i in watch if i.get('instrument')=='FUTIDX' and i['segment']=='NSE_FNO']
    prices={r['key']:r.get('price') for r in board.view()['items']}
    near=[]
    for symbol in ('nifty','banknifty'):
        candidates=[i for i in options if i['underlying']==symbol]
        if candidates and prices.get(symbol):
            expiry=min(i['expiry'] for i in candidates)
            near+=sorted((i for i in candidates if i['expiry']==expiry),key=lambda i:(abs(i['strike']-prices[symbol]),i['side']))[:24]
    depth=futures+near
    one=next((i for i in futures if i.get('key')=='nifty_future'),None)
    scopes={'live':selected[:5000],'live-extra':selected[5000:],'depth20':depth[:50],'depth200':[one] if one else []}
    return {'revision':digest([scopes,credential_revision]),'generated_at':now().isoformat(),'scopes':scopes,
            'live_candidate_count':len(live),'live_capacity_excluded':max(0,len(live)-10000),
            'depth20_candidate_count':len(futures)+len(options),'depth20_unsubscribed':max(0,len(futures)+len(options)-len(scopes['depth20'])),
            'depth20_scope':'Two current index futures and up to 48 nearest front-expiry NIFTY/BANKNIFTY option contracts',
            'depth200_scope':'Exact current NIFTY futures contract; one instrument capacity',
            'scope':'Resolved watch instruments and all current NIFTY/BANKNIFTY options; broader catalogue uses quote/history sweeps',
            'limits':{'live':5000,'depth20':50,'depth200':1},
            'unavailable':['Unresolved watch identities','Foreign feeds absent from verified Dhan master'],
            'all_market_continuous':False}

def start(collector,clock=None):
    clock=clock or now();store=collector.store
    state=replace(store,CONTROL,lambda old:{**(old or {}),'enabled':True,
                  'started_at':(old or {}).get('started_at') or clock.isoformat(),
                  'credential_revision':(old or {}).get('credential_revision') or uuid.uuid4().hex})
    replace(store,'control/gap-recovery',lambda old:{**(old or {}),'enabled':True,'next_scan_at':0})
    # Unattended recordings own the connections. Preserve sampled archives and
    # checkpoints, but do not run competing one-shot socket collectors.
    for job in all_jobs(store):
        if job['state']=='running' and set(job['config']['families'])<=set(('live','depth20','depth200')):
            collector.control(job['id'],'pause')
    return state

def credentials_saved(collector):
    store=collector.store
    if not enabled(store):return {'enabled':False,'resumed':0}
    replace(store,CONTROL,lambda old:{**old,'credential_revision':uuid.uuid4().hex,'credentials_updated_at':now().isoformat()})
    resumed=0
    for job in all_jobs(store):
        if job['state']=='token_required':collector.control(job['id'],'resume');resumed+=1
    return {'enabled':True,'resumed':resumed}

def ensure_job(collector,role,plan,identity,force_fetch=False):
    jid=digest(['data-system-v1',role,identity])[:32]
    result=collector.create(plan,job_id=jid)
    job=collector.get_job(jid)
    if job.get('system_role')!=role or (force_fetch and not job.get('force_fetch')):
        job=replace(collector.store,'jobs/'+jid,lambda old:{**old,'system_role':role,
                    **({'force_fetch':True} if force_fetch else {})})
    if result['state']=='paused' and not job.get('system_user_paused'):
        result=collector.control(jid,'resume')
    return result

def schedule_history(collector,clock=None):
    clock=clock or now();store=collector.store
    closed=recent_closed_day(clock);target=closed+timedelta(days=1)
    catalogue=collector.catalogue()
    if not catalogue:raise ValueError('Instrument catalogue unavailable')
    state=store.document('system/maintenance')[0] or {};slots=dict(state.get('history',{}))
    specs={'core_daily':(['daily'],['NIFTY','BANKNIFTY','INDIA VIX','INDIAVIX'],['IDX_I']),
           'core_intraday':(['intraday'],['NIFTY','BANKNIFTY','INDIA VIX','INDIAVIX'],['IDX_I']),
           'core_rolling':(['rolling'],['NIFTY','BANKNIFTY'],['NSE_FNO']),
           'core_chains':(['chain'],['NIFTY','BANKNIFTY'],['IDX_I']),
           'all_daily':(['daily'],[],[]),'all_intraday':(['intraday'],[],[]),'all_rolling':(['rolling'],[],[])}
    for role,(families,symbols,segments) in specs.items():
        slot=dict(slots.get(role,{}));previous=collector.get_job(slot['job_id']) if slot.get('job_id') else None
        if (role.startswith('core_') and slot.get('schedule_version')!=2
                and (previous is None or previous['state'] in ('completed','no_work'))):
            # A new, scoped plan repairs stale cache reuse and adds VIX/options.
            # Old plans/outcomes retain their own immutable cursor and evidence.
            slot={};previous=None
        if previous and previous['state'] not in ('completed','no_work'):
            slots[role]={**slot,'state':previous['state'],'counts':previous['counts']};continue
        if previous and previous['state']=='no_work':
            slots[role]={**slot,'state':'NO_WORK_REVIEW_REQUIRED'};continue
        attempted=date.fromisoformat(slot['end']) if slot.get('end') else None
        if attempted and attempted>=target:
            slots[role]={**slot,'state':previous['state'],'counts':previous['counts']};continue
        # Overlap recent dates for late EOD publication/corrections. An outage
        # keeps its oldest unresolved range; no day is silently skipped.
        begin=attempted-timedelta(days=2) if attempted else target-timedelta(days=3)
        end=min(target,begin+timedelta(days=30))
        plan=CollectionPlan(start=begin,end=end,families=families,intervals=[1],symbols=symbols,segments=segments,
                            continue_rejected_requests=True,quarantine_invalid_candles=True,
                            rolling_atm_baseline=role=='core_rolling')
        # Overlapping catch-up must ask again. A raw response fetched before the
        # requested day closed cannot establish that day's final coverage.
        job=ensure_job(collector,role,plan,[begin.isoformat(),end.isoformat(),catalogue['sha256'],'fresh-history-v2'],force_fetch=True)
        slots[role]={'job_id':job['id'],'start':begin.isoformat(),'end':end.isoformat(),'state':job['state'],'counts':job['counts'],
                     'schedule_version':2,
                     'scope':('Monthly code 1 ATM CALL/PUT availability baseline; all-strike backfill remains separate' if role=='core_rolling' else
                              'All returned current NIFTY/BANKNIFTY expiries; snapshots, not historical chain coverage' if role=='core_chains' else
                              'Selected catalogue instruments; exact requested-session outcomes'),
                     'attempted_through':slot.get('end'),'verified_through':None,
                     'note':'Scheduled/attempted is not verified coverage. Failed, empty and partial outcomes remain gaps.'}
    replace(store,'system/maintenance',lambda old:{**(old or {}),'history':slots,'closed_day_target':closed.isoformat(),
            'last_schedule_at':clock.isoformat(),'calendar':session(closed)})
    return slots

def schedule_quotes(collector,clock=None):
    clock=clock or now();store=collector.store;state=store.document('system/quote-sweep')[0] or {}
    if not state:
        state=adopt_sweep(collector,'quotes',clock)
    if state.get('job_id'):
        job=collector.get_job(state['job_id'])
        if job['state'] not in ('completed','no_work'):return state
        if clock.timestamp()-state.get('scheduled_at',0)<600:return state
    today=clock.astimezone(IST).date()
    plan=CollectionPlan(start=today,end=today+timedelta(days=1),families=['quotes'],intervals=[1],
                        continue_rejected_requests=True,quarantine_invalid_candles=True)
    job=ensure_job(collector,'quote_sweep',plan,[today.isoformat(),int(clock.timestamp())//600])
    state={'job_id':job['id'],'scheduled_at':clock.timestamp(),'state':job['state'],
           'scope':'Full mapped catalogue sweep; per-instrument timestamps determine freshness. Not simultaneous quotes.'}
    replace(store,'system/quote-sweep',lambda _:state)
    return state

def adopt_sweep(collector,family,clock):
    """Continue an existing whole-catalogue sweep instead of duplicating it."""
    candidates=[j for j in all_jobs(collector.store) if j['state']=='running'
                and j['config']['families']==[family] and not j.get('retry_of')
                and not j['config'].get('symbols') and not j['config'].get('segments')]
    if not candidates:return {}
    job=max(candidates,key=lambda j:j['created_at'])
    state={'job_id':job['id'],'scheduled_at':clock.timestamp(),'state':'running','adopted':True}
    replace(collector.store,'system/'+('quote-sweep' if family=='quotes' else 'chain-sweep'),lambda _:state)
    return state

def bootstrap_history(collector,clock=None):
    clock=clock or now();store=collector.store
    if store.document('system/backfill')[0]:return
    today=clock.astimezone(IST).date()
    try:begin=today.replace(year=today.year-5)
    except ValueError:begin=today.replace(year=today.year-5,day=28)
    jobs=list(all_jobs(store));roots={}
    for family in ('daily','intraday','rolling'):
        candidates=[j for j in jobs if not j.get('retry_of') and family in j['config']['families']
                    and not j['config'].get('symbols') and not j['config'].get('segments')
                    and j['config']['start']<=(begin+timedelta(days=7)).isoformat()
                    and j['config']['end']>=(today-timedelta(days=7)).isoformat()
                    and (family!='rolling' or j.get('rolling_identity_version')==2)]
        if candidates:
            job=sorted(candidates,key=lambda j:(j['state']=='running',j['created_at']),reverse=True)[0]
        else:
            job=ensure_job(collector,'backfill_'+family,CollectionPlan(start=begin,end=today,families=[family],
                           intervals=[1,5,15,25,60],continue_rejected_requests=True,quarantine_invalid_candles=True),
                           [begin.isoformat(),today.isoformat()])
        roots[family]=job['id']
    install_once(store,'system/backfill',{'jobs':roots,'configured_at':clock.isoformat(),
                 'scope':'Pinned catalogue historical backfill; original failures and boundaries retained'})

def schedule_chains(collector,clock=None):
    clock=clock or now();store=collector.store;state=store.document('system/chain-sweep')[0] or {}
    if not state:
        state=adopt_sweep(collector,'chain',clock)
    if state.get('job_id'):
        job=collector.get_job(state['job_id'])
        if job['state'] not in ('completed','no_work'):return state
        if clock.timestamp()-state.get('scheduled_at',0)<900:return state
    today=clock.astimezone(IST).date()
    plan=CollectionPlan(start=today,end=today+timedelta(days=1),families=['chain'],intervals=[1],continue_rejected_requests=True)
    job=ensure_job(collector,'chain_sweep',plan,[today.isoformat(),int(clock.timestamp())//900])
    state={'job_id':job['id'],'scheduled_at':clock.timestamp(),'state':job['state'],
           'scope':'All discovered current underlyings/expiries; rotating snapshots, not simultaneous chain history'}
    replace(store,'system/chain-sweep',lambda _:state)
    return state

def audit_snapshot(collector,clock=None):
    """Job outcomes are evidence of attempts, never a full-coverage percentage."""
    clock=clock or now();jobs=list(all_jobs(collector.store));families={}
    for job in jobs:
        label=','.join(job['config']['families'])
        group=families.setdefault(label,{'jobs':0,'states':{},'attempt_outcomes':{},'rows_saved_across_attempts':0})
        group['jobs']+=1
        state=job['state'];group['states'][state]=group['states'].get(state,0)+1
        for key,count in job.get('counts',{}).items():group['attempt_outcomes'][key]=group['attempt_outcomes'].get(key,0)+count
        group['rows_saved_across_attempts']+=job.get('rows_saved',0)
    cat=collector.catalogue() or {}
    report={'checked_at':clock.isoformat(),'job_groups':families,
            'catalogue_rows':cat.get('rows'),'catalogue_sha256':cat.get('sha256'),
            'unresolved_catalogue_reasons':cat.get('review_reasons',{}),
            'coverage_verified':False,
            'note':'Counts include original and retry attempts, not unique coverage. Completed jobs can contain gaps. Tick/depth loss and provider-unavailable history cannot be fabricated.'}
    replace(collector.store,'system/data-audit',lambda _:report)
    return report

def publish_subscription_plan(store,plan,clock=None):
    """Discovery failure must never replace a working feed scope with emptiness."""
    if not plan.get('revision') or not plan.get('scopes',{}).get('live'):
        raise ValueError('No resolved live identities; existing subscription plan retained')
    clock=clock or now()
    archive='system/subscriptions/'+clock.astimezone(IST).date().isoformat()+'/'+plan['revision']
    # Archive before publishing so every advertised revision is recoverable.
    install_once(store,archive,plan)
    replace(store,'system/last-good-subscription',lambda _:{'key':archive,'revision':plan['revision']})
    replace(store,SCOPE,lambda _:plan)
    replace(store,'system/subscription-revision',lambda _:{'revision':plan['revision']})
    return plan


def load_pinned_instruments(collector,board,catalogue,clock=None):
    """Resolve exact identities from the same immutable master as history jobs.

    Avoid a second public-master download in every restarted worker. Only the
    small watch/index-option page groups are read; no API segment is invented.
    """
    clock=clock or now();today=clock.astimezone(IST).date()
    try:
        fetched=datetime.fromisoformat(catalogue['fetched_at'])
        age=(clock-fetched).total_seconds()
    except (KeyError,TypeError,ValueError):raise ValueError('Pinned catalogue timestamp unavailable') from None
    if not -5<=age<=86400:raise ValueError('Pinned catalogue is stale; identity refresh required')
    sha=catalogue['sha256']
    if getattr(board,'pinned_sha',None)==sha and board.master_day==today and time.monotonic()-board.master_at<21600:return
    groups={'IDX_I-INDEX','NSE_FNO-FUTIDX','NSE_FNO-OPTIDX','MCX_COMM-FUTCOM','NSE_CURRENCY-FUTCUR'}
    pages=[page for page in catalogue['pages'] if page['group'] in groups]
    columns={'EXCH_ID':'exchange','SEGMENT':'raw_segment','SECURITY_ID':'security_id',
             'SYMBOL_NAME':'symbol','UNDERLYING_SYMBOL':'underlying','INSTRUMENT':'instrument',
             'SM_EXPIRY_DATE':'expiry','LOT_SIZE':'lot_size','STRIKE_PRICE':'strike','OPTION_TYPE':'side'}
    text=io.StringIO();writer=csv.DictWriter(text,fieldnames=list(columns));writer.writeheader()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for items in pool.map(lambda page:collector.page_items(page['key']),pages):
            for item in items:
                if not item.get('reason'):writer.writerow({column:item.get(key) for column,key in columns.items()})
    instruments,options=parse_master(text.getvalue(),today)
    if not instruments:raise ValueError('Pinned master has no resolved watch identities')
    board.instruments,board.options=instruments,options
    board.master_day=today;board.master_at=time.monotonic();board.pinned_sha=sha
    board.catalog_error=None


def restore_subscriptions(store,credential_revision,clock=None):
    """Recover retained, unexpired identities without declaring a fresh discovery."""
    clock=clock or now();today=clock.astimezone(IST).date();current=store.document(SCOPE)[0] or {}
    best=current if current.get('scopes',{}).get('live') else None
    if best is None:
        pointer=store.document('system/last-good-subscription')[0] or {}
        key=pointer.get('key','')
        if key.startswith('system/subscriptions/'):
            saved=store.document(key)[0]
            if saved and saved.get('scopes',{}).get('live'):best=saved
    if best is None:
        # Bootstrap compatibility with the previous release, which did not keep
        # a last-good pointer. Bound reads and retain only one candidate in RAM.
        for day in (today,today-timedelta(days=1)):
            for key in store.document_names('system/subscriptions/'+day.isoformat()+'/',limit=64):
                saved=store.document(key)[0] or {}
                if saved.get('scopes',{}).get('live') and (best is None or str(saved.get('generated_at') or '')>str(best.get('generated_at') or '')):best=saved
    if best is None:return None
    scopes={family:[item for item in items if not item.get('expiry') or item['expiry']>=today.isoformat()]
            for family,items in best.get('scopes',{}).items()}
    if not scopes.get('live'):return None
    revision=digest([scopes,credential_revision])
    if current.get('revision')==revision and current.get('scopes')==scopes:return current
    restored={**best,'scopes':scopes,'revision':revision,'restored_at':clock.isoformat(),
              'discovery_state':'RETAINED_IDENTITIES_REFRESH_PENDING',
              'scope_warning':'Retained unexpired identities; source discovery has not yet been refreshed. Capacity counts are from that source.'}
    publish_subscription_plan(store,restored,clock)
    return restored


async def maintenance(collector,stop):
    from dhan_data import fetch_chain
    from session_chart import refresh_session
    board=MarketBoard(collector.provider,collector.store);store=collector.store
    while not stop.is_set() and await asyncio.to_thread(enabled,store):
        cycle_start=time.monotonic();errors={}
        async def failed(stage,error):
            record=await asyncio.to_thread(capture,store,stage,error);errors[stage]=record['id']
        try:
            config=(await asyncio.to_thread(store.document,CONTROL))[0]
            await asyncio.to_thread(restore_subscriptions,store,config['credential_revision'])
        except asyncio.CancelledError:raise
        except Exception as error:await failed('subscription_restore',error)
        try:
            cat=await asyncio.to_thread(collector.catalogue)
            if not cat or cat.get('fetched_at','')[:10]!=now().date().isoformat():
                try:cat=await collector.refresh_catalogue()
                except Exception as error:
                    await failed('catalogue_refresh',error)
                    if not cat:raise
            await asyncio.to_thread(load_pinned_instruments,collector,board,cat)
            await board.refresh()
            config=(await asyncio.to_thread(store.document,CONTROL))[0]
            plan=await asyncio.to_thread(subscription_plan,board,config['credential_revision'])
            plan.update(discovery_state='PINNED_MASTER_RESOLVED',catalogue_sha256=cat['sha256'],
                        identities_source_at=cat['fetched_at'])
            await asyncio.to_thread(publish_subscription_plan,store,plan)
        except asyncio.CancelledError:raise
        except Exception as error:await failed('instrument_discovery',error)
        # History/sweep failures do not stop recording, and discovery failures
        # do not prevent independent daily catch-up from preserving its cursor.
        for stage,operation in (('backfill_schedule',bootstrap_history),('daily_schedule',schedule_history),
                                ('quote_schedule',schedule_quotes),('chain_schedule',schedule_chains),('data_audit',audit_snapshot)):
            try:await asyncio.to_thread(operation,collector)
            except asyncio.CancelledError:raise
            except Exception as error:await failed(stage,error)
        if trading_state(now())=='OPEN':
            for symbol in ('nifty','banknifty'):
                try:
                    await refresh_session(collector.provider,store,board,symbol)
                    if board.instruments.get(symbol):await fetch_chain(collector.provider,store,symbol,board.instruments[symbol])
                except asyncio.CancelledError:raise
                except Exception as error:await failed('current_session',error)
        await asyncio.to_thread(replace,store,'system/maintenance',lambda old:{**(old or {}),
            'state':'DEGRADED' if errors else 'RUNNING','heartbeat':now().isoformat(),'component_errors':errors,
            'error':'Some components need review; independent collection continues. See diagnostics.' if errors else None})
        try:await asyncio.wait_for(stop.wait(),max(1,60-(time.monotonic()-cycle_start)))
        except asyncio.TimeoutError:pass

async def supervise(collector,stop):
    from data_recorder import record
    store=collector.store
    def active():return not stop.is_set() and enabled(store)
    cached={};scope_lock=threading.Lock()
    def scope(family):
        with scope_lock:
            head=store.document('system/subscription-revision')[0] or {}
            if not cached or cached.get('revision')!=head.get('revision'):
                plan=store.document(SCOPE)[0] or {};cached.clear();cached.update(plan)
            return {'revision':cached.get('revision'),'instruments':cached.get('scopes',{}).get(family,[])}
    async def guarded(family):
        while await asyncio.to_thread(active):
            try:await record(collector.provider,store,'live' if family=='live-extra' else family,scope,active,stream_id=family)
            except asyncio.CancelledError:raise
            except Exception as error:
                await asyncio.to_thread(capture,store,'recorder_supervisor',error)
                await asyncio.to_thread(replace,store,'system/recorder/'+family,lambda old:{**(old or {}),
                    'state':'STORAGE_OR_RECORDER_ERROR','heartbeat':now().isoformat(),'error':'Recording interrupted; outage retained, no continuity claim'})
                try:await asyncio.wait_for(stop.wait(),10)
                except asyncio.TimeoutError:pass
    async with asyncio.TaskGroup() as group:
        group.create_task(maintenance(collector,stop))
        for family in ('live','live-extra','depth20','depth200'):group.create_task(guarded(family))

def status(store,clock=None):
    from build_identity import runtime_build
    clock=clock or now();config=store.document(CONTROL)[0] or {};plan=store.document(SCOPE)[0] or {}
    worker=store.document('control/worker')[0] or {}
    lease_held=worker.get('until',0)>clock.timestamp()
    try:worker_age=(clock-datetime.fromisoformat(worker.get('heartbeat'))).total_seconds()
    except (TypeError,ValueError):worker_age=None
    active=lease_held and worker_age is not None and -5<=worker_age<=90
    streams={}
    for family in ('live','live-extra','depth20','depth200'):
        data=store.document('system/recorder/'+family)[0] or {'state':'NOT_STARTED'}
        at=data.get('last_durable_received_at');heartbeat=data.get('heartbeat')
        def age(value):
            try:return (clock-datetime.fromisoformat(value)).total_seconds()
            except (TypeError,ValueError):return None
        data={**data,'durable_receipt_age_seconds':age(at),'heartbeat_age_seconds':age(heartbeat)}
        if not config.get('enabled'):data['state']='PAUSED'
        elif not active:data['state']='WORKER_NOT_ACTIVE'
        elif any(data[k] is not None and data[k]<-5 for k in ('heartbeat_age_seconds','durable_receipt_age_seconds')):data['state']='FUTURE_TIMESTAMP_REVIEW_REQUIRED'
        elif data['heartbeat_age_seconds'] is not None and data['heartbeat_age_seconds']>90:data['state']='STALE_WORKER'
        elif data['durable_receipt_age_seconds'] is not None and data['durable_receipt_age_seconds']>30 and data['state']=='RECORDING':
            data['state']='NO_RECENT_DURABLE_FRAMES'
        streams[family]=data
    return {'enabled':bool(config.get('enabled')),'scope':plan.get('scope'),'subscription_revision':plan.get('revision'),
            'discovery_state':plan.get('discovery_state'),'scope_warning':plan.get('scope_warning'),
            'worker_active':active,'worker_lease_held':lease_held,'worker_heartbeat':worker.get('heartbeat'),
            'worker_build_id':worker.get('build_id'),'expected_worker_build_id':runtime_build(),
            'worker_build_matches':worker.get('build_id')==runtime_build(),
            'subscribed':{k:len(v) for k,v in plan.get('scopes',{}).items()},
            'live_capacity_excluded':plan.get('live_capacity_excluded'),'all_market_continuous':False,
            'depth20_unsubscribed':plan.get('depth20_unsubscribed'),'depth20_scope':plan.get('depth20_scope'),
            'streams':streams,'maintenance':store.document('system/maintenance')[0] or {},
            'quote_sweep':store.document('system/quote-sweep')[0] or {},
            'chain_sweep':store.document('system/chain-sweep')[0] or {},
            'backfill':store.document('system/backfill')[0] or {},
            'audit':store.document('system/data-audit')[0] or {},
            'diagnostics':store.document('system/diagnostics')[0] or {},
            'watchdog':store.document('control/data-watchdog')[0] or {'verified':False},
            'calendar':session(clock.astimezone(IST).date()),'server_time':clock.isoformat(),
            'completion':'Not certified. Review scope, gaps, timestamps, calendar and historical outcomes.'}
