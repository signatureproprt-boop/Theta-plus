#!/usr/bin/env python3
"""Activate the verified collector image without deleting data or stealing leases.

Run after release-existing.sh. Stops only executions of the configured collector,
waits for their termination and for the existing 600s durable leases to expire.
No storage, IAM, secrets, task counts or environment values are changed.
"""
import json
import re
import subprocess
import time
from sync_worker_image import gcloud, JOB, job_image


def execution_terminal(entry):
    status=entry.get('status',{})
    if status.get('completionTime'):return True
    # Failed/cancelled executions can have a terminal Completed condition
    # before completionTime is populated. Zero running tasks alone is not proof.
    return any(c.get('type')=='Completed' and c.get('status') in ('True','False')
               for c in status.get('conditions',[]))


def active_executions():
    executions=json.loads(gcloud('run','jobs','executions','list','--job='+JOB,'--format=json'))
    active=[]
    for entry in executions:
        name=entry['metadata']['name']
        if not re.fullmatch(re.escape(JOB)+r'-[a-z0-9]+',name):
            raise ValueError('Unexpected collector execution identity; handover stopped')
        if not execution_terminal(entry):active.append(name)
    return active


def durable_lease_released():
    """Public read-only evidence, pinned to the deployed source fingerprint."""
    from check_worker_release import URL,runtime_build
    from urllib.request import urlopen,Request
    try:
        with urlopen(Request(URL,headers={'Cache-Control':'no-cache'}),timeout=8) as response:data=json.load(response)
        return (data.get('worker_lease_held') is False and data.get('worker_active') is False
                and data.get('expected_worker_build_id')==runtime_build())
    except Exception:return False


def activate(expected_image,lease_check=None):
    if '@sha256:' not in expected_image:raise ValueError('Immutable image required')
    def verify():
        job=json.loads(gcloud('run','jobs','describe',JOB,'--format=json'))
        if job_image(job)!=expected_image:raise ValueError('Collector image changed; activation stopped')
    verify()
    active=active_executions()
    for name in active:
        try:gcloud('run','jobs','executions','cancel',name,'--no-async','--quiet')
        except subprocess.CalledProcessError:
            # It may have finished between list and cancel. Accept only fresh
            # terminal evidence; permission/network failures still stop rollout.
            entry=json.loads(gcloud('run','jobs','executions','describe',name,'--format=json'))
            if entry.get('metadata',{}).get('name')!=name or not execution_terminal(entry):raise
    deadline=time.monotonic()+180
    while active_executions():
        if time.monotonic()>=deadline:raise RuntimeError('Collector still running; no new execution started')
        time.sleep(5)
    # Also handles a lease left by a recently terminated execution. Do not
    # delete or edit it: the last checkpoint remains authoritative.
    for remaining in range(610,0,-10):
        # A graceful stop releases its own durable lease. Read that evidence;
        # never delete a lease, or assume a stopped execution released it.
        if lease_check and lease_check():
            print('Durable collector lease release verified; expiry wait is no longer required.',flush=True)
            break
        if remaining%60==10:print(f'Worker handover: waiting {remaining}s for durable leases.',flush=True)
        time.sleep(10)
    verify()
    if active_executions():
        # The existing private watchdog can win this race after lease expiry.
        # Do not cancel or duplicate it; the next rollout gate checks its build.
        print('Collector already started during handover; running build acceptance is required.')
        return
    gcloud('run','jobs','execute',JOB,'--async','--quiet')
    print('New collector execution requested on the verified image; live outcomes still require verification.')


if __name__=='__main__':
    revision=json.loads(gcloud('run','services','describe','theta-plus','--format=json'))
    traffic=[t for t in revision['status']['traffic'] if t.get('percent',0)>0]
    if len(traffic)!=1 or traffic[0]['percent']!=100:raise SystemExit('One verified serving revision required')
    candidate=json.loads(gcloud('run','revisions','describe',traffic[0]['revisionName'],'--format=json'))
    if not re.fullmatch(r'theta-plus-f380-[0-9]{14}',traffic[0]['revisionName']):raise SystemExit('Collector repair release must be serving first')
    activate(candidate['status']['imageDigest'],lease_check=durable_lease_released)
