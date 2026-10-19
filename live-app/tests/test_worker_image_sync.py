import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('sync_worker_image',Path(__file__).parents[1]/'deploy/sync_worker_image.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def job(image):return {'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':image,'env':[{'name':'SECRET','value':'not-printed'}]}]}}}}}}

def revision():return {'spec':{'containers':[{'image':'repo@sha256:new','env':[{'name':'THETA_COLLECTION_WORKER','value':m.RESOURCE}]}]},'status':{'imageDigest':'repo@sha256:new'}}

def test_update_only_existing_job_image_and_verify(monkeypatch,capsys):
    calls=[];current=['repo@sha256:old']
    def run(*args):
        calls.append(args)
        if 'describe' in args:return json.dumps(job(current[0]))
        current[0]=next(a.removeprefix('--image=') for a in args if a.startswith('--image='));return ''
    monkeypatch.setattr(m,'gcloud',run)
    assert m.sync(revision())['state']=='VERIFIED'
    updates=[c for c in calls if 'update' in c]
    assert updates==[('run','jobs','update',m.JOB,'--image=repo@sha256:new','--quiet')]
    assert not any('execute' in c or 'replace' in c for c in calls)
    assert 'not-printed' not in capsys.readouterr().out


def test_worker_verification_failure_restores_old_image(monkeypatch):
    updates=[]
    def run(*args):
        if 'describe' in args:return json.dumps(job('repo@sha256:old'))
        updates.append(args);return ''
    monkeypatch.setattr(m,'gcloud',run)
    with pytest.raises(RuntimeError,match='previous image restored'):m.sync(revision())
    assert '--image=repo@sha256:old' in updates[-1]


def test_no_worker_or_wrong_resource_never_changes_jobs(monkeypatch):
    monkeypatch.setattr(m,'gcloud',lambda *a:pytest.fail('No job mutation allowed'))
    rev=revision();rev['spec']['containers'][0]['env']=[]
    assert m.sync(rev)['state']=='NOT_CONFIGURED'
    rev=revision();rev['spec']['containers'][0]['env'][0]['value']='projects/other/locations/region/jobs/other'
    with pytest.raises(ValueError):m.sync(rev)
