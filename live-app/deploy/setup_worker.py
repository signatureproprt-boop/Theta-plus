"""Configure a private Cloud Run collection job from the deployed service identity.

Keeps the existing service, bucket and Dhan secrets. Does not read secret values.
"""
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


def main():
    service=json.loads(gcloud('run','services','describe',SERVICE,'--format=json'))
    traffic=[t for t in service['status']['traffic'] if t.get('percent',0)>0]
    if len(traffic)!=1 or traffic[0]['percent']!=100:raise ValueError('Expected one verified serving revision')
    revision=json.loads(gcloud('run','revisions','describe',traffic[0]['revisionName'],'--format=json'))
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
    print('Private collection worker configured. Existing Admin authorization controls Resume.')
    print('No collection is started automatically. Saved plans remain unchanged.')

if __name__=='__main__':main()
