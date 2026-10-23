from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from data_system import load_pinned_instruments, subscription_plan, LIVE_STREAMS
from store import Store
from collection import Collector

CLOCK=datetime(2026,10,23,4,tzinfo=timezone.utc)

def instrument(key,sid,segment='NSE_EQ',kind='EQUITY',**kw):
    return {'key':key,'security_id':str(sid),'exchange':'NSE','raw_segment':'I' if kind=='INDEX' else 'E',
            'segment':segment,'instrument':kind,'symbol':'NIFTY' if kind=='INDEX' else key,'display':key,
            'expiry':'1900-01-01' if kind=='EQUITY' else None,'reason':None,**kw}

def test_pinned_catalogue_supplies_all_mapped_segments_without_inventing_identities(tmp_path,monkeypatch):
    import data_system
    monkeypatch.setattr(data_system,'now',lambda:CLOCK)
    watch=instrument('nifty',13,'IDX_I','INDEX')
    groups={'IDX_I-INDEX':[watch],
            'NSE_EQ-EQUITY':[instrument('RELIANCE',2885)],
            'BSE_EQ-EQUITY':[instrument('BSE',2885,segment='BSE_EQ')],
            'MCX_COMM-FUTCOM':[instrument('GOLD',999,'MCX_COMM','FUTCOM',exchange='MCX',raw_segment='M',expiry='2026-12-01')],
            'NSE_M-FUTCOM':[instrument('unmapped',1000,segment=None,reason='No documented mapping')],
            'NSE_FNO-OPTSTK':[instrument('old',1001,'NSE_FNO','OPTSTK',expiry='2020-01-01')]}
    store=Store(tmp_path/'db.sqlite3');pages=[]
    for n,(group,items) in enumerate(groups.items()):
        key='pages/'+str(n);store.put_document(key,{'items':items},0);pages.append({'key':key,'group':group})
    cat={'sha256':'a'*64,'fetched_at':CLOCK.isoformat(),'pages':pages}
    board=SimpleNamespace(master_day=None,master_at=0,view=lambda:{'items':[]})
    load_pinned_instruments(Collector(None,store),board,cat,CLOCK)
    plan=subscription_plan(board)
    rows=[i for f in LIVE_STREAMS for i in plan['scopes'][f]]
    assert {(i['segment'],i['security_id']) for i in rows}=={('IDX_I','13'),('NSE_EQ','2885'),('BSE_EQ','2885'),('MCX_COMM','999')}
    assert len(rows)==4 and rows[0]['key']=='nifty'
    assert plan['live_candidate_count']==4 and plan['live_capacity_excluded']==0
    # Prefer primary NSE cash identities when the broader universe exceeds capacity.
    assert [i['segment'] for i in board.live_catalogue if i['instrument']=='EQUITY']==['NSE_EQ','BSE_EQ']
    with pytest.raises(ValueError):load_pinned_instruments(Collector(None,store),board,{**cat,'fetched_at':'2026-10-01T00:00:00+00:00'},CLOCK)

def test_four_bounded_shards_preserve_core_identity_and_report_overflow(monkeypatch):
    import data_system
    monkeypatch.setattr(data_system,'now',lambda:CLOCK)
    core=instrument('nifty',13,'IDX_I','INDEX')
    board=SimpleNamespace(instruments={'nifty':core},options={},live_catalogue=[core]+[
        instrument('stock'+str(n),100+n) for n in range(21000)],live_catalogue_total=21001,view=lambda:{'items':[]})
    plan=subscription_plan(board,'one')
    assert [len(plan['scopes'][f]) for f in LIVE_STREAMS]==[5000]*4
    rows=[i for f in LIVE_STREAMS for i in plan['scopes'][f]]
    assert rows[0]['key']=='nifty' and len({(i['segment'],i['security_id']) for i in rows})==20000
    assert plan['live_capacity_excluded']==1001 and plan['interactive_connection_reserved']
    assert not plan['all_market_continuous']
    assert subscription_plan(board,'one')['feed_revisions']==plan['feed_revisions']
