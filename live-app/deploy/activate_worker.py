#!/usr/bin/env python3
"""Activate the verified collector image without deleting data or stealing leases.

Run after release-existing.sh. Stops only executions of the configured collector,
waits for their termination and for the existing 600s durable leases to expire.
No storage, IAM, secrets, task counts or environment values are changed.
"""
import json
import re
import time
from sync_worker_image import gcloud, JOB, job_image


def active_executions():
    executions=json.loads(gcloud('run','jobs','executions','list','--job='+JOB,'--format=json'))
    active=[]
    for entry in executions:
        name=entry['metadata']['name']
        if not re.fullmatch(re.escape(JOB)+r'-[a-z0-9]+',name):
            raise ValueError('Unexpected collector execution identity; handover stopped')
        if not entry.get('status',{}).get('completionTime'):active.append(name)
    return active


def activate(expected_image):
    if '@sha256:' not in expected_image:raise ValueError('Immutable image required')
    def verify():
        job=json.loads(gcloud('run','jobs','describe',JOB,'--format=json'))
        if job_image(job)!=expected_image:raise ValueError('Collector image changed; activation stopped')
    verify()
    active=active_executions()
    for name in active:
        gcloud('run','jobs','executions','cancel',name,'--no-async','--quiet')
    deadline=time.monotonic()+180
    while active_executions():
        if time.monotonic()>=deadline:raise RuntimeError('Collector still running; no new execution started')
        time.sleep(5)
    # Also handles a lease left by a recently terminated execution. Do not
    # delete or edit it: the last checkpoint remains authoritative.
    for remaining in range(610,0,-10):
        if remaining%60==10:print(f'Worker handover: waiting {remaining}s for durable leases.',flush=True)
        time.sleep(10)
    verify()
    if active_executions():raise RuntimeError('Another collector started; activation stopped to avoid overlap')
    gcloud('run','jobs','execute',JOB,'--async','--quiet')
    print('New collector execution requested on the verified image; live outcomes still require verification.')


if __name__=='__main__':
    revision=json.loads(gcloud('run','services','describe','theta-plus','--format=json'))
    traffic=[t for t in revision['status']['traffic'] if t.get('percent',0)>0]
    if len(traffic)!=1 or traffic[0]['percent']!=100:raise SystemExit('One verified serving revision required')
    candidate=json.loads(gcloud('run','revisions','describe',traffic[0]['revisionName'],'--format=json'))
    if not re.fullmatch(r'theta-plus-f380-[0-9]{14}',traffic[0]['revisionName']):raise SystemExit('Collector repair release must be serving first')
    activate(candidate['status']['imageDigest'])
