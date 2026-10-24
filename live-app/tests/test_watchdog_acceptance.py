import sys,json
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'deploy'))
import verify_watchdog as m

CLOCK=datetime(2026,10,24,8,tzinfo=timezone.utc)

def scheduler_job():
    return {'name':'projects/theta-pluse/locations/asia-south1/jobs/theta-watchdog','state':'ENABLED','schedule':'*/5 * * * *',
        'httpTarget':{'uri':'https://run.googleapis.com/v2/projects/theta-pluse/locations/asia-south1/jobs/theta-plus-collector:run',
            'httpMethod':'POST','oauthToken':{'serviceAccountEmail':'watchdog@example.invalid'}},
        'lastAttemptTime':'2026-10-24T08:01:00Z','status':{'code':0}}


def harness(monkeypatch):
    state={'cancelled':False};calls=[];reports=[]
    data={'enabled':True,'worker_active':True,'worker_build_matches':True,'worker_build_id':'build','expected_worker_build_id':'build','calendar':{'state':'CLOSED'}}
    checkpoint={'a'*32:{'items_assessed':5,'rows_saved':200,'catalogue_sha256':'master','config':{'start':'2026-10-21'}}}
    monkeypatch.setattr(m,'runtime_build',lambda:'build')
    monkeypatch.setattr(m,'read_status',lambda:{**deepcopy(data),'worker_execution':m.JOB+('-new' if state['cancelled'] else '-old')})
    monkeypatch.setattr(m,'checkpoints',lambda snapshot:deepcopy(checkpoint))
    monkeypatch.setattr(m,'active_executions',lambda:[m.JOB+('-new' if state['cancelled'] else '-old')])
    monkeypatch.setattr(m,'scheduler',lambda *args:json.dumps([scheduler_job()] if args[0]=='list' else scheduler_job()))
    def cloud(*args):
        calls.append(args)
        if 'cancel' in args:state['cancelled']=True
        return ''
    monkeypatch.setattr(m,'gcloud',cloud);monkeypatch.setattr(m,'persist',reports.append)
    return state,calls,reports,data,checkpoint


def test_natural_restart_requires_scheduler_success_matching_runtime_and_checkpoints(monkeypatch):
    state,calls,reports,data,checkpoint=harness(monkeypatch)
    m.verify(CLOCK)
    assert reports[0]['verified'] and reports[0]['checkpoint_verified'] and reports[0]['restart_observed']
    assert reports[0]['basis']=='natural-scheduler-restart-v1' and reports[0]['checkpoints_checked']==1
    assert any('cancel' in c for c in calls) and not any('execute' in c for c in calls)


@pytest.mark.parametrize('change',[{'state':'PAUSED'},{'schedule':'* * * * *'},{'httpTarget':{'uri':'https://wrong.invalid'}}])
def test_wrong_or_paused_scheduler_never_stops_collector(monkeypatch,change):
    _,calls,reports,_,_=harness(monkeypatch)
    monkeypatch.setattr(m,'scheduler',lambda *args:json.dumps([{**scheduler_job(),**change}]))
    with pytest.raises(ValueError,match='One exact'):m.verify(CLOCK)
    assert calls==[] and reports==[]


def test_manual_fallback_cannot_pass_acceptance(monkeypatch):
    state,calls,reports,data,_=harness(monkeypatch)
    original=m.gcloud
    def cloud(*args):
        result=original(*args)
        if 'cancel' in args:data['worker_active']=False
        return result
    monkeypatch.setattr(m,'gcloud',cloud)
    clock=iter([0,721]);monkeypatch.setattr(m.time,'monotonic',lambda:next(clock))
    import check_worker_release
    monkeypatch.setattr(check_worker_release,'verify',lambda:True)
    with pytest.raises(RuntimeError,match='timed out'):m.verify(CLOCK)
    assert any('execute' in c for c in calls) and reports==[]


def test_checkpoint_regression_and_old_scheduler_attempt_are_rejected():
    before={'x':{'items_assessed':2,'rows_saved':10,'catalogue_sha256':'a','config':{'start':'2026-10-21'}}}
    after=deepcopy(before);after['x']['rows_saved']=9
    assert not m.preserved(before,after)
    job=scheduler_job();job['lastAttemptTime']='2026-10-24T07:59:00Z'
    assert not m.scheduler_success_after(job,CLOCK)
    job=scheduler_job();job['status']['code']=7
    assert not m.scheduler_success_after(job,CLOCK)


def test_outside_authorized_day_does_not_touch_cloud(monkeypatch):
    _,calls,reports,_,_=harness(monkeypatch)
    m.verify(datetime(2026,10,25,8,tzinfo=timezone.utc))
    assert calls==[] and reports==[]


def test_recorded_evidence_requires_fresh_exact_scoped_proof():
    from watchdog_evidence import verified,RESOURCE
    r={'verified':True,'basis':'natural-scheduler-restart-v1','worker':RESOURCE,'restart_observed':True,
       'checkpoint_verified':True,'checkpoints_checked':5,'checked_at':CLOCK.isoformat()}
    assert verified(r,CLOCK)
    for change in ({'basis':'manual-fallback'},{'checkpoint_verified':False},{'checkpoints_checked':True},
        {'worker':'other'},{'checked_at':'2026-10-01T00:00:00+00:00'},{'checked_at':'2026-10-25T00:00:00+00:00'}):
        assert not verified({**r,**change},CLOCK)


def test_old_worker_identity_cannot_certify_a_new_execution(monkeypatch):
    state,calls,reports,data,_=harness(monkeypatch)
    monkeypatch.setattr(m,'read_status',lambda:{**data,'worker_execution':m.JOB+'-old'})
    ticks=iter([0,1,721]);monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(m.time,'sleep',lambda _:None)
    with pytest.raises(RuntimeError,match='timed out'):m.verify(CLOCK)
    assert reports==[]


def test_append_only_receipt_updates_display_without_overwriting_control(tmp_path):
    from watchdog_evidence import load,RESOURCE,verified
    from store import Store
    s=Store(tmp_path/'db')
    s.put_document('control/data-watchdog',{'reported_configured':True,'verified':False},0)
    report={'verified':True,'basis':'natural-scheduler-restart-v1','worker':RESOURCE,'restart_observed':True,
       'checkpoint_verified':True,'checkpoints_checked':5,'checked_at':CLOCK.isoformat()}
    s.add('watchdog_acceptance',report)
    loaded=load(s,CLOCK)
    assert verified(loaded,CLOCK) and loaded['evidence_storage']=='append-only acceptance receipt'
    assert s.document('control/data-watchdog')[0]['verified'] is False
    assert not verified(load(s,datetime(2026,11,2,tzinfo=timezone.utc)),datetime(2026,11,2,tzinfo=timezone.utc))


def test_invalid_or_unreadable_receipts_do_not_create_positive_evidence(tmp_path,monkeypatch):
    from watchdog_evidence import load
    from store import Store
    s=Store(tmp_path/'db');s.put_document('control/data-watchdog',{'verified':False},0)
    s.add('watchdog_acceptance',{'verified':True,'basis':'manual-fallback'})
    assert load(s,CLOCK)=={'verified':False}
    def fail(*args):raise OSError('Temporary read failure')
    monkeypatch.setattr(s,'rows',fail)
    assert load(s,CLOCK)=={'verified':False}


def test_receipt_upload_is_create_only_and_verified_by_readback(monkeypatch):
    class Result:pass
    calls=[];record={}
    def cloud(*args):return json.dumps({'spec':{'template':{'spec':{'template':{'spec':{'containers':[{'env':[{'name':'THETA_GCS_BUCKET','value':'test-data'}]}]}}}}}})
    def run(args,**kwargs):
        calls.append(args);record.update(json.loads(Path(args[3]).read_text()));return Result()
    def output(args,**kwargs):
        calls.append(args);return json.dumps(record)
    monkeypatch.setattr(m,'gcloud',cloud);monkeypatch.setattr(m.subprocess,'run',run)
    monkeypatch.setattr(m.subprocess,'check_output',output)
    m.persist({'verified':True})
    assert '--if-generation-match=0' in calls[0]
    assert '/records/watchdog_acceptance/' in calls[0][4]
    assert not any('/control/' in part for call in calls for part in call)
    assert record['data']=={'verified':True} and 'cat' in calls[1]
