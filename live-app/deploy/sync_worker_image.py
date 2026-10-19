#!/usr/bin/env python3
"""Update only an existing collector's image to the verified serving revision.

No IAM, credentials, env, task count or execution changes. Existing executions
finish on their original image; the next execution uses the updated image.
"""
import json
import subprocess
import sys

PROJECT='theta-pluse';REGION='asia-south1';JOB='theta-plus-collector'
RESOURCE=f'projects/{PROJECT}/locations/{REGION}/jobs/{JOB}'

def gcloud(*args):
    return subprocess.check_output(['gcloud',*args,'--project='+PROJECT,'--region='+REGION],text=True)

def job_image(job):
    return job['spec']['template']['spec']['template']['spec']['containers'][0]['image']

def sync(revision):
    env={e['name']:e.get('value') for e in revision['spec']['containers'][0].get('env',[])}
    resource=env.get('THETA_COLLECTION_WORKER')
    if not resource:
        print('Collection worker not configured; no job was created or changed.')
        return {'state':'NOT_CONFIGURED'}
    if resource!=RESOURCE:raise ValueError('Unexpected collection worker resource; job unchanged')
    image=revision.get('status',{}).get('imageDigest') or revision['spec']['containers'][0]['image']
    if '@sha256:' not in image:raise ValueError('Verified immutable candidate image required')
    before=json.loads(gcloud('run','jobs','describe',JOB,'--format=json'));old=job_image(before)
    if old==image:
        print('Collection worker already uses the verified image.')
        return {'state':'VERIFIED','image':image}
    changed=False
    try:
        # Only --image is changed. Existing job env/secrets/IAM remain configured.
        changed=True;gcloud('run','jobs','update',JOB,'--image='+image,'--quiet')
        after=json.loads(gcloud('run','jobs','describe',JOB,'--format=json'))
        if job_image(after)!=image:raise ValueError('Worker image verification failed')
    except Exception:
        if changed:
            try:gcloud('run','jobs','update',JOB,'--image='+old,'--quiet')
            except Exception:raise RuntimeError('Worker image rollback failed; inspect the existing collector job') from None
        raise RuntimeError('Worker image sync failed; previous image restored') from None
    print('Collection worker image verified. New executions use this release; an existing execution keeps its original image.')
    return {'state':'VERIFIED','image':image}

if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Expected verified revision JSON file')
    with open(sys.argv[1]) as stream:sync(json.load(stream))
