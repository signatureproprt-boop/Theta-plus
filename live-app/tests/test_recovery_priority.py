from gap_recovery import select_recovery_source,tick,CONTROL,SCAN_POLICY
from collection import Collector,CollectionPlan,replace
from store import Store
from test_collection import Provider,seed


def job(id,role):return {'id':id,'system_role':role}


def test_recent_history_gets_three_turns_without_starving_archive():
    sources=[job('a','all_daily'),job('b','all_intraday'),job('c','all_daily_earlier'),job('d','backfill_daily'),job('e','backfill_rolling')]
    state={};selected=[]
    for _ in range(8):
        parent,positions,priority,wrapped=select_recovery_source(sources,state)
        state.update(positions);selected.append(parent['id']);assert priority
    assert selected==['a','b','c','d','a','b','c','e']
    # State survives restart and stale positions after a job disappears.
    parent,positions,_,_=select_recovery_source(sources[:1],{'priority_position':999,'history_scan_turn':3})
    assert parent['id']=='a' and positions['priority_position']==0


def test_general_only_history_remains_round_robin():
    sources=[job('a','backfill_daily'),job('b','backfill_rolling')];state={};selected=[]
    for _ in range(3):
        parent,positions,priority,wrapped=select_recovery_source(sources,state)
        state.update(positions);selected.append(parent['id']);assert not priority
    assert selected==['a','b','a']


def test_quote_jobs_do_not_consume_historical_scan_turns_and_pause_is_preserved(tmp_path,monkeypatch):
    c=Collector(Provider(),Store(tmp_path/'db'));seed(c.store)
    quote=c.create(CollectionPlan(start='2026-10-18',end='2026-10-21',families=['quotes']))
    c.control(quote['id'],'resume')
    history=c.create(CollectionPlan(start='2026-10-18',end='2026-10-21',families=['daily']))
    c.control(history['id'],'resume')
    replace(c.store,'jobs/'+history['id'],lambda j:{**j,'system_role':'all_daily'})
    c.store.put_document(CONTROL,{'enabled':True,'scan_policy':SCAN_POLICY,'cursors':{'retained':'position'}},0)
    seen=[]
    monkeypatch.setattr(c,'results',lambda jid,after='':seen.append(jid) or {'items':[]})
    tick(c);state=c.store.document(CONTROL)[0]
    assert seen==[history['id']] and state['progress']['history_sources']==1
    assert state['cursors']['retained']=='position' and state['next_scan_at']==0
    c.control(history['id'],'pause');seen.clear();tick(c)
    assert seen==[] and c.get_job(history['id'])['system_user_paused']
