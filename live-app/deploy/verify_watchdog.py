"""One authorized closed-market scheduler recovery test, without IAM/config edits.

Stops only the existing collector after validating its exact Scheduler target.
Waits for natural Scheduler dispatch; manual fallback cannot pass acceptance.
"""
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date,datetime,timezone
from pathlib import Path
from urllib.request import Request,urlopen
from activate_worker import active_executions,execution_terminal
from check_worker_release import URL,runtime_build
from sync_worker_image import gcloud,JOB,PROJECT,REGION

AUTHORIZED_DAY=date(2026,10,24)


def read_status():
    with urlopen(Request(URL,headers={'Cache-Control':'no-cache'}),timeout=15) as response:return json.load(response)


def scheduler(*args):
    return subprocess.check_output(['gcloud','scheduler','jobs',*args,'--project='+PROJECT,'--location='+REGION],text=True,stderr=subprocess.PIPE)


def target_matches(job):
    uri=job.get('httpTarget',{}).get('uri','')
    allowed={f'https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run',
        f'https://{REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{PROJECT}/jobs/{JOB}:run',
        f'https://{REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/261331732835/jobs/{JOB}:run',
        f'https://run.googleapis.com/v2/projects/261331732835/locations/{REGION}/jobs/{JOB}:run'}
    return (uri in allowed and job.get('state')=='ENABLED' and job.get('schedule')=='*/5 * * * *'
        and job.get('httpTarget',{}).get('httpMethod')=='POST' and bool(job.get('httpTarget',{}).get('oauthToken',{}).get('serviceAccountEmail')))


def checkpoints(snapshot,fetch=None):
    ids=set(snapshot.get('backfill',{}).get('jobs',{}).values())
    ids.update(p['job_id'] for p in snapshot.get('whole_market',{}).get('plans',[]) if p.get('role','').startswith('equity_recent'))
    if not ids:raise ValueError('No saved historical checkpoints to verify')
    result={}
    for jid in sorted(ids):
        if not re.fullmatch('[a-f0-9]{32}',jid):raise ValueError('Unexpected checkpoint identity')
        if fetch:data=fetch(jid)
        else:
            with urlopen(URL.replace('/api/data-system','/api/collection/jobs/'+jid),timeout=20) as response:data=json.load(response)
        if data.get('id')!=jid:raise ValueError('Checkpoint identity mismatch')
        result[jid]={k:data.get(k) for k in ('items_assessed','rows_saved','catalogue_sha256','config')}
    return result


def preserved(before,after):
    return bool(before) and set(before)==set(after) and all(
        after[j]['items_assessed']>=v['items_assessed'] and after[j]['rows_saved']>=v['rows_saved']
        and after[j]['catalogue_sha256']==v['catalogue_sha256'] and after[j]['config']==v['config'] for j,v in before.items())


def scheduler_success_after(job,started):
    try:
        at=datetime.fromisoformat(job['lastAttemptTime'].replace('Z','+00:00'))
        return at>=started and job.get('status',{}).get('code',0)==0
    except (KeyError,ValueError,TypeError):return False


def runtime_verified(data):
    return (data.get('enabled') is True and data.get('worker_active') is True and data.get('worker_build_matches') is True
        and data.get('worker_build_id')==runtime_build() and data.get('expected_worker_build_id')==runtime_build())


def persist(report):
    # Read only the configured bucket name; never print runtime configuration.
    job=json.loads(gcloud('run','jobs','describe',JOB,'--format=json'))
    containers=job['spec']['template']['spec']['template']['spec']['containers']
    bucket=next((e.get('value') for e in containers[0].get('env',[]) if e['name']=='THETA_GCS_BUCKET'),None)
    if not bucket or not re.fullmatch('[a-z0-9][a-z0-9_.-]{1,220}',bucket):raise ValueError('Configured durable bucket unavailable')
    # Append an immutable receipt. Overwriting the old control status requires
    # storage.objects.delete; the verifier needs only create and read access.
    record_id=int(time.time()*1000)*1000+secrets.randbelow(1000)
    record={'id':record_id,'created_at':datetime.now(timezone.utc).isoformat(),'data':report}
    key=f'{9007199254740991-record_id:016d}'
    uri=f'gs://{bucket}/records/watchdog_acceptance/{key}.json'
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'evidence.json';path.write_text(json.dumps(record))
        subprocess.run(['gcloud','storage','cp',str(path),uri,'--if-generation-match=0','--quiet'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        saved=json.loads(subprocess.check_output(['gcloud','storage','cat',uri],text=True))
        if saved!=record:raise ValueError('Acceptance receipt readback did not match')



def verify(clock=None):
    from zoneinfo import ZoneInfo
    local=(clock or datetime.now(timezone.utc)).astimezone(ZoneInfo('Asia/Kolkata'))
    if local.date()!=AUTHORIZED_DAY:
        print('WATCHDOG_TEST_SKIPPED: one-time authorized date only; existing evidence retained.');return
    data=read_status()
    if data.get('watchdog',{}).get('restart_test_verified') and data['watchdog'].get('basis')=='natural-scheduler-restart-v1':
        print('WATCHDOG_ALREADY_VERIFIED; no collector interruption.');return
    if data.get('calendar',{}).get('state')!='CLOSED' or not runtime_verified(data):
        raise ValueError('Closed market and healthy matching collector required; nothing stopped')
    try:jobs=json.loads(scheduler('list','--format=json'))
    except subprocess.CalledProcessError:
        raise RuntimeError('Scheduler read permission unavailable; collector was not stopped') from None
    matches=[j for j in jobs if target_matches(j)]
    if len(matches)!=1:raise ValueError('One exact authenticated enabled collector Scheduler required; nothing stopped')
    matched=matches[0];name=matched['name'].rsplit('/',1)[-1]
    if not re.fullmatch('[a-zA-Z0-9_-]+',name):raise ValueError('Unexpected scheduler identity')
    before=checkpoints(data);old=set(active_executions())
    if not old:raise ValueError('No active collector; nothing stopped')
    started=(clock or datetime.now(timezone.utc)).astimezone(timezone.utc);recovered=False
    try:
        for execution in sorted(old):
            try:gcloud('run','jobs','executions','cancel',execution,'--no-async','--quiet')
            except subprocess.CalledProcessError:
                state=json.loads(gcloud('run','jobs','executions','describe',execution,'--format=json'))
                if not execution_terminal(state):raise
        deadline=time.monotonic()+720
        while time.monotonic()<deadline:
            active=set(active_executions());current=read_status()
            job=json.loads(scheduler('describe',name,'--format=json'))
            if (not active.intersection(old) and active-old and runtime_verified(current) and current.get('worker_execution') in active-old
                    and target_matches(job) and scheduler_success_after(job,started)):
                after=checkpoints({'backfill':{'jobs':{str(n):jid for n,jid in enumerate(before)}}})
                # New jobs may be added by maintenance; compare exactly the
                # retained original IDs, without requiring the new set to match.
                after={j:after[j] for j in before if j in after}
                if not preserved(before,after):raise ValueError('Historical checkpoint preservation not verified')
                recovered=True
                report={'reported_configured':True,'verified':True,'basis':'natural-scheduler-restart-v1',
                    'schedule':job['schedule'],'worker':f'projects/{PROJECT}/locations/{REGION}/jobs/{JOB}',
                    'checked_at':datetime.now(timezone.utc).isoformat(),'test_started_at':started.isoformat(),
                    'scheduler':name,'scheduler_last_attempt':job['lastAttemptTime'],'restart_observed':True,
                    'checkpoint_verified':True,'checkpoints_checked':len(before),'previous_executions':sorted(old),
                    'replacement_executions':sorted(active-old),
                    'note':'Natural Scheduler dispatch restored a matching collector after controlled execution cancellation. Saved historical counters/configurations were preserved. This does not certify market-open feed continuity or full historical coverage.'}
                persist(report);print('WATCHDOG_NATURAL_RESTART_AND_CHECKPOINTS_VERIFIED',len(before));return
            time.sleep(10)
        raise RuntimeError('Natural Scheduler restart acceptance timed out')
    finally:
        if not recovered:
            # Restore service if the test fails. No failed run can certify the
            # watchdog merely because this manual fallback recovered recording.
            try:
                latest=read_status();healthy=runtime_verified(latest);user_paused=latest.get('enabled') is False
            except Exception:healthy=False;user_paused=False
            if not healthy and not user_paused:
                gcloud('run','jobs','execute',JOB,'--async','--quiet')
                from check_worker_release import verify as verify_release
                verify_release()
            print('WATCHDOG_NOT_CERTIFIED; collector restored or already healthy.')


if __name__=='__main__':verify()
