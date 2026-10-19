"""Configure a private Cloud Run collection job from the deployed service identity.

Keeps the existing service, bucket and Dhan secrets. Does not read secret values.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

PROJECT='theta-pluse'
REGION='asia-south1'
SERVICE='theta-plus'
JOB='theta-plus-collector'
RESOURCE=f'projects/{PROJECT}/locations/{REGION}/jobs/{JOB}'


def gcloud(*args):
    return subprocess.check_output(['gcloud',*args,'--project='+PROJECT,'--region='+REGION],text=True)


def job_spec(revision):
    spec=revision['spec'];container=spec['containers'][0]
    allowed={'THETA_GCS_BUCKET','THETA_DHAN_CREDENTIALS_SECRET','THETA_DHAN_TOKEN_SECRET','DHAN_CLIENT_ID','DHAN_ACCESS_TOKEN'}
    env=[e for e in container.get('env',[]) if e['name'] in allowed]
    names={e['name'] for e in env}
    if 'THETA_GCS_BUCKET' not in names:raise ValueError('Durable bucket missing')
    if not names.intersection({'THETA_DHAN_CREDENTIALS_SECRET','THETA_DHAN_TOKEN_SECRET','DHAN_ACCESS_TOKEN'}):raise ValueError('Dhan credentials configuration missing')
    env.append({'name':'THETA_COLLECTION_WORKER','value':RESOURCE})
    image=revision.get('status',{}).get('imageDigest') or container['image']
    if '@sha256:' not in image:raise ValueError('An immutable deployed image digest is required')
    if not spec.get('serviceAccountName'):raise ValueError('Existing service identity missing')
    return {'apiVersion':'run.googleapis.com/v1','kind':'Job','metadata':{'name':JOB},
        'spec':{'template':{'spec':{'taskCount':1,'parallelism':1,'template':{'spec':{
            'serviceAccountName':spec['serviceAccountName'],'timeoutSeconds':'86400','maxRetries':1,
            'containers':[{'image':image,'command':['python'],'args':['worker.py'],'env':env,
                           'resources':{'limits':{'cpu':'1','memory':'1Gi'}}}]}}}}}}


def traffic(service):
    return {t['revisionName']:t['percent'] for t in service['status']['traffic'] if t.get('percent',0)>0}


def activate_worker(before,expected_image=None):
    """Explicit traffic is pinned by release-existing.sh; env update doesn't move it."""
    current=json.loads(gcloud('run','services','describe',SERVICE,'--format=json'))
    name=current['status']['latestReadyRevisionName']
    revision=json.loads(gcloud('run','revisions','describe',name,'--format=json'))
    env={e['name']:e.get('value') for e in revision['spec']['containers'][0].get('env',[])}
    if env.get('THETA_COLLECTION_WORKER')!=RESOURCE:raise ValueError('Latest ready revision has no expected worker configuration; traffic unchanged')
    image=revision.get('status',{}).get('imageDigest') or revision['spec']['containers'][0]['image']
    if expected_image and image!=expected_image:raise ValueError('Unexpected worker-enabled image; traffic unchanged')
    if traffic(current)!=traffic(before):raise ValueError('Traffic changed concurrently; activation stopped')
    if not any(c.get('type')=='Ready' and c.get('status')=='True' for c in revision['status'].get('conditions',[])):
        raise ValueError('Worker-enabled revision is not ready')
    rollback=','.join(f'{k}={v}' for k,v in sorted(traffic(before).items()))
    gcloud('run','services','update-traffic',SERVICE,'--to-revisions='+name+'=100','--quiet')
    try:
        from verify_rollout import wait_for_health
        wait_for_health(before['status']['url'],'theta-foundation-3.2.0',name)
    except Exception:
        gcloud('run','services','update-traffic',SERVICE,'--to-revisions='+rollback,'--quiet')
        raise RuntimeError('Worker revision verification failed; previous traffic restored') from None
    print('Worker-enabled serving revision verified:',name)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate-only',action='store_true',help='Activate the already-created worker-enabled revision without rebuilding')
    args=parser.parse_args()
    service=json.loads(gcloud('run','services','describe',SERVICE,'--format=json'))
    routes=[t for t in service['status']['traffic'] if t.get('percent',0)>0]
    if len(routes)!=1 or routes[0]['percent']!=100:raise ValueError('Expected one verified serving revision')
    revision=json.loads(gcloud('run','revisions','describe',routes[0]['revisionName'],'--format=json'))
    image=revision.get('status',{}).get('imageDigest') or revision['spec']['containers'][0]['image']
    if args.activate_only:
        activate_worker(service,image)
        return
    payload=job_spec(revision)
    with tempfile.TemporaryDirectory(prefix='theta-worker-') as work:
        path=Path(work)/'worker.json'
        path.write_text(json.dumps(payload));os.chmod(path,0o600)
        # May contain env values; keep the manifest private and do not print it.
        subprocess.run(['gcloud','run','jobs','replace',str(path),'--project='+PROJECT,'--region='+REGION,'--quiet'],check=True)
    identity=revision['spec']['serviceAccountName']
    subprocess.run(['gcloud','run','jobs','add-iam-policy-binding',JOB,'--project='+PROJECT,'--region='+REGION,
                    '--member=serviceAccount:'+identity,'--role=roles/run.invoker','--quiet','--format=none'],check=True)
    subprocess.run(['gcloud','run','services','update',SERVICE,'--project='+PROJECT,'--region='+REGION,
                    '--update-env-vars=THETA_COLLECTION_WORKER='+RESOURCE,'--quiet'],check=True)
    activate_worker(service,image)
    print('Private collection worker configured. Existing Admin authorization controls Resume.')
    print('No collection is started automatically. Saved plans remain unchanged.')

if __name__=='__main__':main()
