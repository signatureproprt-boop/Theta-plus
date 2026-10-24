"""Cloud Run Job worker: continue durable collection without a browser/password."""
import asyncio
import os
import re
import time
import uuid
import signal
from datetime import datetime
from runtime import IST
from fastapi import HTTPException
from collection import Collector,replace,now
from credentials import DhanCredentials
from provider import Dhan
from diagnostics import capture
from build_identity import runtime_build
from google.api_core.exceptions import Aborted,DeadlineExceeded,ServiceUnavailable,TooManyRequests,InternalServerError

class WorkerLeaseLost(RuntimeError):pass

RESOURCE=re.compile(r'projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+')

def worker_status(store):
    data=store.document('control/worker')[0] or {}
    return {'configured':bool(RESOURCE.fullmatch(os.environ.get('THETA_COLLECTION_WORKER',''))),
            'active':data.get('until',0)>time.time(),
            'build_id':data.get('build_id'),'expected_build_id':runtime_build(),
            'build_matches':data.get('build_id')==runtime_build(),
            'heartbeat':data.get('heartbeat'),'state':data.get('state','idle')}

def launch_worker(store):
    """Called only after Admin authorization. Uses service identity, never Dhan auth."""
    if (store.document('control/migration')[0] or {}).get('state') in ('FENCED','NEW_BACKEND'):
        return {'mode':'unified_backend','state':'migrated'}
    resource=os.environ.get('THETA_COLLECTION_WORKER','')
    if not RESOURCE.fullmatch(resource):raise HTTPException(503,'Background worker is not configured')
    if worker_status(store)['active']:return {'mode':'cloud_run_job','state':'active'}
    claimed=False;owner=uuid.uuid4().hex
    def claim(old):
        nonlocal claimed
        claimed=False
        if old and old.get('until',0)>time.time():return old
        claimed=True;return {'owner':owner,'until':time.time()+90}
    replace(store,'control/dispatch',claim)
    if not claimed:return {'mode':'cloud_run_job','state':'starting'}
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        with AuthorizedSession(credentials) as session:
            result=session.post('https://run.googleapis.com/v2/'+resource+':run',json={},timeout=20,allow_redirects=False)
            if not 200<=result.status_code<300:raise RuntimeError('Worker dispatch rejected')
        return {'mode':'cloud_run_job','state':'starting'}
    except Exception:
        replace(store,'control/dispatch',lambda old:None if old and old.get('owner')==owner else old)
        raise HTTPException(503,'Background worker could not start. Check Cloud Run Job and its invoker permission; saved checkpoint retained.') from None


def resume_legacy_budget_pause(store,job,clock=None):
    """Migrate only explicit pre-upgrade quota pauses after their IST day ends."""
    clock=clock or datetime.now(IST)
    messages={'Daily API budget reached','Daily market-data request budget reached'}
    def eligible(value):
        if not value or value.get('state')!='paused' or value.get('system_user_paused') or value.get('message') not in messages:return False
        try:
            updated=datetime.fromisoformat(value['updated_at'])
            return updated.tzinfo is not None and updated.astimezone(IST).date()<clock.astimezone(IST).date()
        except (KeyError,ValueError,TypeError):return False
    if not eligible(job):return job
    return replace(store,'jobs/'+job['id'],lambda old:{**old,'state':'running',
        'message':'Daily quota rollover: resuming the retained checkpoint.','updated_at':now()} if eligible(old) else old)

def running_jobs(store):
    from gap_recovery import all_jobs
    jobs=[resume_legacy_budget_pause(store,j) for j in all_jobs(store)]
    jobs=[j for j in jobs if j.get('state')=='running']
    jobs.sort(key=lambda j:(0 if str(j.get('system_role') or '').startswith('core_') else 1,j['id']))
    return iter(j['id'] for j in jobs)

def pause_failed_job(store,jid,error):
    record=capture(store,'collection_job',error,jid)
    transient=isinstance(error,(TimeoutError,ConnectionError,Aborted,DeadlineExceeded,ServiceUnavailable,TooManyRequests,InternalServerError))
    def pause(old):
        if not isinstance(old,dict):return old
        if transient:
            return {**old,'retry_at':time.time()+60,'system_error':record,
                    'message':'Temporary storage/transport failure. Same checkpoint retained; automatic retry after backoff. Diagnostic '+record['id']}
        return {**old,'state':'paused','system_review_required':True,'system_error':record,
                'message':'Internal collection error. Checkpoint retained; review diagnostic '+record['id']+'. Other data components continue.'}
    try:replace(store,'jobs/'+jid,pause)
    except Exception as storage_error:capture(store,'job_pause_storage',storage_error,jid)
    return record


async def run_worker(collector,max_seconds=23*3600,clock=None):
    clock=clock or time.monotonic
    store=collector.store;owner=uuid.uuid4().hex;claimed=False
    execution=os.environ.get('CLOUD_RUN_EXECUTION')
    def claim(old):
        nonlocal claimed
        claimed=False
        # Cloud Run starts a retry only after the previous attempt has stopped.
        # Reclaim that execution's stale lease after a hard kill; other executions
        # remain excluded. The deployed job has exactly one task.
        same_execution=bool(execution and old and old.get('execution')==execution)
        if old and old.get('until',0)>time.time() and not same_execution:return old
        claimed=True;return {'owner':owner,'execution':execution,'until':time.time()+600,'heartbeat':now(),
                             'build_id':runtime_build(),'state':'running'}
    await asyncio.to_thread(replace,store,'control/worker',claim)
    if not claimed:return
    started=clock()
    def heartbeat(state='running'):
        def touch(old):
            if not old or old.get('owner')!=owner:raise WorkerLeaseLost('Worker lease lost')
            return {**old,'heartbeat':now(),'until':time.time()+600 if state=='running' else 0,'state':state}
        replace(store,'control/worker',touch)
    state='idle'
    stop=asyncio.Event()
    async def keep_lease():
        while not stop.is_set():
            await asyncio.to_thread(heartbeat)
            try:await asyncio.wait_for(stop.wait(),30)
            except asyncio.TimeoutError:pass
    async def data_engine():
        from data_system import enabled,supervise
        while not stop.is_set():
            try:
                if await asyncio.to_thread(enabled,store):await supervise(collector,stop)
            except asyncio.CancelledError:raise
            except Exception as error:await asyncio.to_thread(capture,store,'data_engine',error)
            try:await asyncio.wait_for(stop.wait(),5)
            except asyncio.TimeoutError:pass
    async def doctor_engine():
        from system_doctor import tick
        while not stop.is_set():
            try:await asyncio.to_thread(tick,collector)
            except asyncio.CancelledError:raise
            except Exception as error:await asyncio.to_thread(capture,store,'system_doctor',error)
            try:await asyncio.wait_for(stop.wait(),2)
            except asyncio.TimeoutError:pass
    lease_task=asyncio.create_task(keep_lease());engine_task=asyncio.create_task(data_engine());doctor_task=asyncio.create_task(doctor_engine())
    next_scan=0;next_job_scan=0;jobs=[];last_stage='worker_start'
    try:
        while clock()-started<max_seconds:
            if lease_task.done():lease_task.result()
            from gap_recovery import tick
            last_stage='worker_heartbeat'
            await asyncio.to_thread(heartbeat)
            recovery_pending=False
            if clock()>=next_scan:
                try:
                    last_stage='gap_recovery'
                    recovery_pending=await asyncio.to_thread(tick,collector)
                    next_scan=clock()+30
                except Exception as error:
                    await asyncio.to_thread(capture,store,'gap_recovery',error)
                    next_scan=clock()+60
            # Active IDs are a scheduling hint only. Collector.run reads fresh
            # state/leases before every request, so external pauses still win.
            # Avoid reloading thousands of completed GCS jobs on every turn.
            if clock()>=next_job_scan or recovery_pending:
                try:
                    last_stage='job_listing'
                    jobs=await asyncio.to_thread(lambda:list(running_jobs(store)))
                    next_job_scan=clock()+60
                except Exception as error:
                    await asyncio.to_thread(capture,store,'job_listing',error)
                    await asyncio.sleep(5);continue
            if not jobs:
                if recovery_pending:continue
                from data_system import enabled
                from system_doctor import pending
                if await asyncio.to_thread(lambda:enabled(store) or pending(store)):
                    await asyncio.sleep(5);continue
                break
            finished=set()
            for jid in jobs:
                if clock()-started>=max_seconds:break
                await asyncio.to_thread(heartbeat)
                try:
                    last_stage='collection_job'
                    result=await collector.run(jid)
                    print('collection',jid,result['state'],result['items_assessed'],flush=True)
                    if result['state']!='running':finished.add(jid)
                except HTTPException as error:
                    if error.status_code not in (409,429):
                        await asyncio.to_thread(pause_failed_job,store,jid,error)
                except Exception as error:
                    await asyncio.to_thread(pause_failed_job,store,jid,error)
            jobs=[jid for jid in jobs if jid not in finished]
            await asyncio.sleep(1)
        else:state='checkpointed'
    except Exception as error:
        state='interrupted'
        record=await asyncio.to_thread(capture,store,last_stage,error)
        raise RuntimeError('Collection worker interrupted; diagnostic '+record['id']) from None
    finally:
        stop.set();engine_task.cancel();doctor_task.cancel()
        await asyncio.gather(engine_task,lease_task,doctor_task,return_exceptions=True)
        try:await asyncio.to_thread(heartbeat,state)
        except WorkerLeaseLost as error:await asyncio.to_thread(capture,store,'worker_lease_lost',error)
        else:await asyncio.to_thread(replace,store,'control/dispatch',lambda _:None)
    # Continue long backfills in a new bounded execution after releasing our lease.
    from data_system import enabled
    from system_doctor import pending
    if state in ('idle','checkpointed') and await asyncio.to_thread(lambda:bool(list(running_jobs(store))) or enabled(store) or pending(store)):
        await asyncio.to_thread(launch_worker,store)


async def serve(collector):
    """Let Cloud Run SIGTERM drain checkpoints and release our own lease."""
    loop=asyncio.get_running_loop();task=asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM,task.cancel)
    try:await run_worker(collector)
    except asyncio.CancelledError:pass
    finally:loop.remove_signal_handler(signal.SIGTERM)


def main():
    bucket=os.environ.get('THETA_GCS_BUCKET')
    if not bucket:raise RuntimeError('Worker requires the existing durable GCS bucket')
    from cloud_store import CloudStore
    store=CloudStore(bucket)
    if (store.document('control/migration')[0] or {}).get('state') in ('FENCED','NEW_BACKEND'):
        print('COLLECTOR_MIGRATED_TO_UNIFIED_BACKEND');return
    asyncio.run(serve(Collector(Dhan(DhanCredentials(),store=store),store)))

if __name__=='__main__':main()
