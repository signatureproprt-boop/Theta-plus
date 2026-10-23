import importlib.util
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'deploy'))
spec=importlib.util.spec_from_file_location('activate_worker',Path(__file__).parents[1]/'deploy/activate_worker.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def test_handover_cancels_only_collector_waits_then_executes(monkeypatch):
    calls=[];sleeps=[];active=[True]
    def run(*args):
        calls.append(args)
        if 'describe' in args:return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
        if 'list' in args:return json.dumps([{'metadata':{'name':m.JOB+'-abc12'},'status':{} if active[0] else {'completionTime':'now'}}])
        if 'cancel' in args:active[0]=False
        return ''
    monkeypatch.setattr(m,'gcloud',run);monkeypatch.setattr(m.time,'sleep',sleeps.append)
    m.activate('repo@sha256:new')
    assert sum(sleeps)==610
    assert ('run','jobs','executions','cancel',m.JOB+'-abc12','--no-async','--quiet') in calls
    assert calls[-1]==('run','jobs','execute',m.JOB,'--async','--quiet')
    assert not any('delete' in c or 'update' in c for c in calls)

def test_wrong_image_or_execution_prevents_activation(monkeypatch):
    calls=[]
    def run(*args):
        calls.append(args)
        return json.dumps([{'metadata':{'name':'other-job-abc'},'status':{}}])
    monkeypatch.setattr(m,'gcloud',run)
    with pytest.raises(ValueError):m.active_executions()
    with pytest.raises(ValueError):m.activate('mutable:latest')
    assert not any('execute' in c or 'cancel' in c for c in calls)

def test_overlapping_new_execution_is_never_cancelled_or_duplicated(monkeypatch):
    count=[0];calls=[]
    def active():
        count[0]+=1
        return ['new-collector'] if count[0]==3 else []
    monkeypatch.setattr(m,'active_executions',active)
    monkeypatch.setattr(m.time,'sleep',lambda _:None)
    def run(*args):
        calls.append(args)
        return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
    monkeypatch.setattr(m,'gcloud',run)
    m.activate('repo@sha256:new')
    assert not any('execute' in c or 'cancel' in c for c in calls)

def test_verified_durable_lease_release_avoids_unnecessary_market_gap(monkeypatch):
    calls=[];sleeps=[]
    monkeypatch.setattr(m,'active_executions',lambda:[])
    monkeypatch.setattr(m.time,'sleep',sleeps.append)
    def run(*args):
        calls.append(args)
        return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
    monkeypatch.setattr(m,'gcloud',run)
    checks=iter([False,True])
    m.activate('repo@sha256:new',lease_check=lambda:next(checks))
    assert sleeps==[10] and calls[-1]==('run','jobs','execute',m.JOB,'--async','--quiet')

@pytest.mark.parametrize('lease,active,build,expected',[(False,False,'fixture',True),(True,False,'fixture',False),(False,True,'fixture',False),(False,False,'old',False),(None,False,'fixture',False)])
def test_public_release_probe_requires_exact_negative_lease_and_matching_build(monkeypatch,lease,active,build,expected):
    import io
    import urllib.request
    import check_worker_release
    monkeypatch.setattr(check_worker_release,'runtime_build',lambda:'fixture')
    data={'worker_lease_held':lease,'worker_active':active,'expected_worker_build_id':build}
    def fetch(request,timeout):
        assert request.full_url==check_worker_release.URL and timeout==8
        return io.StringIO(json.dumps(data))
    monkeypatch.setattr(urllib.request,'urlopen',fetch)
    assert m.durable_lease_released() is expected
