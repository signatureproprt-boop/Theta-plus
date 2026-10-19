"""Cloud Run Job worker: continue durable collection without a browser/password."""
import asyncio
import os
import re
import time
import uuid
from fastapi import HTTPException
from collection import Collector,replace,now
from credentials import DhanCredentials
from provider import Dhan

RESOURCE=re.compile(r'projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+')

def worker_status(store):
    data=store.document('control/worker')[0] or {}
    return {'configured':bool(RESOURCE.fullmatch(os.environ.get('THETA_COLLECTION_WORKER',''))),
            'active':data.get('until',0)>time.time(),
            'heartbeat':data.get('heartbeat'),'state':data.get('state','idle')}

def launch_worker(store):
    """Called only after Admin authorization. Uses service identity, never Dhan auth."""
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


def running_jobs(store):
    cursor=''
    while True:
        names=store.document_names('jobs/',cursor,100)
        for name in names:
            job=store.document(name)[0]
            if job and job.get('state')=='running':yield job['id']
        if len(names)<100:return
        cursor=names[-1]

async def run_worker(collector,max_seconds=23*3600):
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
        claimed=True;return {'owner':owner,'execution':execution,'until':time.time()+600,'heartbeat':now(),'state':'running'}
    await asyncio.to_thread(replace,store,'control/worker',claim)
    if not claimed:return
    started=time.monotonic()
    def heartbeat(state='running'):
        def touch(old):
            if not old or old.get('owner')!=owner:raise RuntimeError('Worker lease lost')
            return {**old,'heartbeat':now(),'until':time.time()+600 if state=='running' else 0,'state':state}
        replace(store,'control/worker',touch)
    state='idle'
    try:
        while time.monotonic()-started<max_seconds:
            jobs=await asyncio.to_thread(lambda:list(running_jobs(store)))
            if not jobs:break
            for jid in jobs:
                if time.monotonic()-started>=max_seconds:break
                await asyncio.to_thread(heartbeat)
                try:
                    result=await collector.run(jid)
                    print('collection',jid,result['state'],result['items_assessed'],flush=True)
                except HTTPException as exc:
                    if exc.status_code not in (409,429):raise
            await asyncio.sleep(3.5)
        else:state='checkpointed'
    except Exception:
        state='interrupted'
        # Do not log credentials, request payloads or credential-bearing URLs.
        print('Worker interrupted; durable checkpoints retained.',flush=True)
        raise RuntimeError('Collection worker interrupted') from None
    finally:
        await asyncio.to_thread(heartbeat,state)
        await asyncio.to_thread(replace,store,'control/dispatch',lambda _:None)
    # Continue long backfills in a new bounded execution after releasing our lease.
    if state in ('idle','checkpointed') and await asyncio.to_thread(lambda:bool(list(running_jobs(store)))):
        await asyncio.to_thread(launch_worker,store)


def main():
    bucket=os.environ.get('THETA_GCS_BUCKET')
    if not bucket:raise RuntimeError('Worker requires the existing durable GCS bucket')
    from cloud_store import CloudStore
    asyncio.run(run_worker(Collector(Dhan(DhanCredentials()),CloudStore(bucket))))

if __name__=='__main__':main()
