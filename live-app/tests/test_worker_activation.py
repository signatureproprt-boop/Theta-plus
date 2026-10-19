"""A worker env update must explicitly promote its ready revision."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('worker_activation',Path(__file__).parents[1]/'deploy/setup_worker.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.fixture
def state(monkeypatch):
    before={'status':{'url':'https://theta.test','traffic':[{'revisionName':'sdk-revision','percent':100}]}}
    current=copy.deepcopy(before);current['status']['latestReadyRevisionName']='worker-revision'
    revision={'spec':{'containers':[{'image':'repo@sha256:fixed','env':[{'name':'THETA_COLLECTION_WORKER','value':m.RESOURCE}]}]},
              'status':{'conditions':[{'type':'Ready','status':'True'}]}}
    calls=[];checks=[]
    def gcloud(*args):
        calls.append(args)
        if args[:3]==('run','services','describe'):return json.dumps(current)
        if args[:3]==('run','revisions','describe'):return json.dumps(revision)
        return '{}'
    monkeypatch.setattr(m,'gcloud',gcloud)
    monkeypatch.setitem(sys.modules,'verify_rollout',SimpleNamespace(wait_for_health=lambda *a:checks.append(a)))
    return before,current,revision,calls,checks


def test_promotes_exact_worker_revision_even_when_traffic_is_pinned(state):
    before,current,revision,calls,checks=state
    m.activate_worker(before,'repo@sha256:fixed')
    assert '--to-revisions=worker-revision=100' in calls[-1]
    assert checks==[('https://theta.test','theta-foundation-3.2.0','worker-revision')]


@pytest.mark.parametrize('reason',['missing_env','wrong_image','traffic_changed','not_ready'])
def test_rejects_unexpected_latest_revision_without_switching_traffic(state,reason):
    before,current,revision,calls,checks=state
    if reason=='missing_env':revision['spec']['containers'][0]['env']=[]
    if reason=='wrong_image':revision['spec']['containers'][0]['image']='repo@sha256:other'
    if reason=='traffic_changed':current['status']['traffic']=[{'revisionName':'someone-else','percent':100}]
    if reason=='not_ready':revision['status']['conditions']=[]
    with pytest.raises(ValueError):m.activate_worker(before,'repo@sha256:fixed')
    assert not any('update-traffic' in c for c in calls) and not checks


def test_restores_traffic_when_worker_revision_health_fails(state,monkeypatch):
    before,current,revision,calls,checks=state
    def fail(*args):raise RuntimeError('health failed')
    monkeypatch.setitem(sys.modules,'verify_rollout',SimpleNamespace(wait_for_health=fail))
    with pytest.raises(RuntimeError,match='restored'):m.activate_worker(before,'repo@sha256:fixed')
    assert '--to-revisions=sdk-revision=100' in calls[-1]
