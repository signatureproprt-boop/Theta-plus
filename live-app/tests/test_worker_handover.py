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

@pytest.mark.parametrize('status,terminal',[
    ({'conditions':[{'type':'Completed','status':'False','reason':'Cancelled'}]},True),
    ({'conditions':[{'type':'Completed','status':'True'}]},True),
    ({'runningCount':0,'conditions':[{'type':'Completed','status':'Unknown'}]},False),
    ({'runningCount':0},False),
])
def test_terminal_condition_without_completion_timestamp(status,terminal):
    assert m.execution_terminal({'status':status}) is terminal

@pytest.mark.parametrize('terminal',[True,False])
def test_cancel_race_requires_fresh_terminal_evidence(monkeypatch,terminal):
    import subprocess
    calls=[];count=[0]
    def active():
        count[0]+=1
        return [m.JOB+'-abc12'] if count[0]==1 else []
    def run(*args):
        calls.append(args)
        if 'cancel' in args:raise subprocess.CalledProcessError(1,['gcloud'])
        if 'executions' in args and 'describe' in args:
            return json.dumps({'metadata':{'name':m.JOB+'-abc12'},'status':{'conditions':[{'type':'Completed','status':'False' if terminal else 'Unknown'}]}})
        return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
    monkeypatch.setattr(m,'active_executions',active)
    monkeypatch.setattr(m,'gcloud',run)
    monkeypatch.setattr(m.time,'sleep',lambda _:None)
    if terminal:m.activate('repo@sha256:new',lease_check=lambda:True)
    else:
        with pytest.raises(subprocess.CalledProcessError):m.activate('repo@sha256:new',lease_check=lambda:True)
    assert any('execute' in c for c in calls) is terminal


def test_watchdog_start_during_old_execution_shutdown_does_not_block_or_duplicate(monkeypatch):
    old=m.JOB+'-old';new=m.JOB+'-new';calls=[];sleeps=[]
    snapshots=iter([[old],[old,new],[new],[new]])
    monkeypatch.setattr(m,'active_executions',lambda:next(snapshots))
    monkeypatch.setattr(m.time,'sleep',sleeps.append)
    def run(*args):
        calls.append(args)
        return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
    monkeypatch.setattr(m,'gcloud',run)
    m.activate('repo@sha256:new')
    assert sleeps==[5]
    assert [c for c in calls if 'cancel' in c]==[('run','jobs','executions','cancel',old,'--no-async','--quiet')]
    assert not any('execute' in c for c in calls)


def test_new_execution_never_hides_an_original_execution_that_has_not_stopped(monkeypatch):
    old=m.JOB+'-old';calls=[]
    snapshots=iter([[old],[old,m.JOB+'-new']])
    monkeypatch.setattr(m,'active_executions',lambda:next(snapshots))
    ticks=iter([0,181]);monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    def run(*args):
        calls.append(args)
        return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
    monkeypatch.setattr(m,'gcloud',run)
    with pytest.raises(RuntimeError,match='still running'):m.activate('repo@sha256:new')
    assert not any('execute' in c for c in calls)


def test_verified_runtime_continues_without_cancelling_healthy_collector(monkeypatch):
    calls=[]
    def run(*args):
        calls.append(args)
        return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'image':'repo@sha256:new'}]}}}}}})
    monkeypatch.setattr(m,'gcloud',run)
    monkeypatch.setattr(m,'active_executions',lambda:pytest.fail('Healthy matching runtime must keep recording'))
    m.activate('repo@sha256:new',release_check=lambda:True)
    assert len(calls)==1 and 'describe' in calls[0]
    with pytest.raises(ValueError):m.activate('repo@sha256:wrong',release_check=lambda:True)


@pytest.mark.parametrize('change,verified',[({},True),({'worker_active':False},False),({'worker_build_matches':False},False),({'worker_build_id':'old'},False),({'enabled':False},False)])
def test_running_release_requires_enabled_active_matching_source(monkeypatch,change,verified):
    import check_worker_release
    monkeypatch.setattr(check_worker_release,'runtime_build',lambda:'fixture')
    monkeypatch.setattr(m,'worker_evidence',lambda:{'enabled':True,'worker_active':True,
        'worker_build_matches':True,'worker_build_id':'fixture',**change})
    assert m.running_release_verified() is verified
