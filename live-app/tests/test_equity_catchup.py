from datetime import datetime,timedelta,timezone
from copy import deepcopy
from collection import replace
from equity_catchup import schedule,KEY,SEGMENTS
from test_whole_market import setup

CLOCK=datetime(2026,10,24,8,tzinfo=timezone.utc)

def test_recent_equities_start_independently_and_do_not_include_commodity(tmp_path):
    c,roots,items=setup(tmp_path);before={f:deepcopy(c.get_job(jid)) for f,jid in roots.items()}
    report=schedule(c,CLOCK)
    for family,slot in report['families'].items():
        job=c.get_job(slot['latest_job_id'])
        assert job['config']['start']=='2026-10-21' and job['config']['end']=='2026-10-24'
        assert job['config']['segments']==SEGMENTS and job['config']['symbols']==[]
        assert job['config']['families']==[family] and job['state']=='running'
        assert [p['group'] for p in job['pages']]==['IDX_I-INDEX','NSE_EQ-EQUITY','BSE_EQ-EQUITY']
    assert all(c.get_job(jid)==before[f] for f,jid in roots.items()) and not report['coverage_verified']
    ids=c.store.document_names('jobs/');schedule(c,CLOCK+timedelta(days=1))
    assert c.store.document_names('jobs/')==ids


def test_bounded_backlog_keeps_original_dates_until_capacity_frees(tmp_path):
    c,_,_=setup(tmp_path);first=schedule(c,CLOCK-timedelta(days=6))
    second=schedule(c,CLOCK-timedelta(days=3))
    third=schedule(c,CLOCK)
    for family,slot in third['families'].items():
        assert slot['state']=='BACKLOG' and slot['active_jobs']==2 and slot['scheduled_to']=='2026-10-20'
        jid=first['families'][family]['latest_job_id']
        replace(c.store,'jobs/'+jid,lambda j:{**j,'state':'completed'})
    fourth=schedule(c,CLOCK)
    for family,slot in fourth['families'].items():
        job=c.get_job(slot['latest_job_id'])
        assert job['config']['start']=='2026-10-20' and job['config']['end']=='2026-10-23'
        assert slot['active_jobs']==2 and slot['completed_jobs']==1 and len(slot['job_ids'])==2


def test_manual_pause_prevents_more_jobs_for_that_family(tmp_path):
    c,_,_=setup(tmp_path);report=schedule(c,CLOCK-timedelta(days=3))
    jid=report['families']['intraday']['latest_job_id'];c.control(jid,'pause')
    after=schedule(c,CLOCK)
    assert after['families']['intraday']['state']=='MANUALLY_PAUSED'
    assert after['families']['intraday']['job_ids']==[jid]
    assert c.get_job(jid)['system_user_paused']
    assert after['families']['daily']['active_jobs']==2


def test_checkpoint_failure_reuses_pinned_jobs(tmp_path,monkeypatch):
    import equity_catchup as module
    import pytest
    c,_,_=setup(tmp_path);original=module.replace
    def interrupted(store,key,fn):
        if key==KEY:raise OSError('Checkpoint unavailable')
        return original(store,key,fn)
    monkeypatch.setattr(module,'replace',interrupted)
    with pytest.raises(OSError):schedule(c,CLOCK)
    ids=c.store.document_names('jobs/');assert len(ids)==5
    monkeypatch.setattr(module,'replace',original);schedule(c,CLOCK)
    assert c.store.document_names('jobs/')==ids
