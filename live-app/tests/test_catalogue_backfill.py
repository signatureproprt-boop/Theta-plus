import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import pytest
from collection import Collector,CollectionPlan,replace
from catalogue_backfill import KEY,schedule,identity
from data_system import bootstrap_history
from store import Store
from test_collection import instrument,Provider,candles

CLOCK=datetime(2026,10,23,15,tzinfo=timezone.utc)
def master(store,sha,items,rolling=()):
    pages=[]
    for n,(group,values) in enumerate([('IDX_I-INDEX',items),('rolling-underlyings',list(rolling))]):
        if not values:continue
        key='catalogue/'+sha+'/pages/'+str(n).zfill(5)
        assert store.put_document(key,{'items':values},0)
        pages.append({'group':group,'key':key,'count':len(values)})
    cat={'sha256':sha,'rows':len(items),'pages':pages,'fetched_at':CLOCK.isoformat()}
    assert store.put_document('catalogue/'+sha+'/manifest',cat,0)
    replace(store,'catalogue/current',lambda _:cat)
    return cat

def setup(tmp_path):
    s=Store(tmp_path/'db');p=Provider();c=Collector(p,s)
    master(s,'a'*64,[instrument()])
    roots={}
    for family in ('daily','intraday','rolling'):
        job=c.create(CollectionPlan(start='2021-10-23',end='2026-10-23',families=[family],intervals=[1]))
        c.control(job['id'],'resume');roots[family]=job['id']
    s.put_document('system/backfill',{'jobs':roots},0)
    return s,c,p,roots

def test_updated_master_backfills_only_added_identities_and_keeps_old_cursors(tmp_path,monkeypatch):
    s,c,p,roots=setup(tmp_path)
    before={f:deepcopy(c.get_job(j)) for f,j in roots.items()}
    # A reordered source row gives the same instrument a new row key.
    master(s,'b'*64,[instrument(key='new-row'),instrument(key='new',security_id='99',symbol='NEW')])
    bootstrap_history(c,CLOCK)
    state=s.document(KEY)[0]
    for f in ('daily','intraday'):
        slot=state['families'][f];job=c.get_job(slot['job_id'])
        assert slot['added_identities']==1 and job['state']=='running'
        assert c.select_item(job)[1]['security_id']=='99'
        assert c.get_job(roots[f])==before[f]
    assert state['families']['rolling']['added_identities']==0
    assert not state['coverage_verified']
    ids=s.document_names('jobs/');bootstrap_history(c,CLOCK)
    assert s.document_names('jobs/')==ids
    async def call(owner,path,body):return candles('2026-10-21')
    monkeypatch.setattr(c,'call',call)
    # Actual source candles use the existing validation/storage pipeline.
    jid=state['families']['daily']['job_id'];result=asyncio.run(c.run(jid))
    assert result['rows_saved']==1
    assert c.results(jid)['items'][0]['task']['item']['security_id']=='99'


def test_changed_mapping_reused_id_and_exchange_are_distinct(tmp_path):
    s,c,p,roots=setup(tmp_path)
    master(s,'b'*64,[instrument(),instrument(key='other-exchange',exchange='BSE'),
        instrument(key='other-contract',expiry='2026-12-30',instrument='FUTIDX',segment='NSE_FNO'),
        instrument(key='new-map',segment='NSE_EQ',instrument='EQUITY')])
    schedule(c,CLOCK)
    assert s.document(KEY)[0]['families']['daily']['added_identities']==3
    assert identity(instrument())==identity(instrument(key='different-row'))


def test_second_update_does_not_requeue_previous_additions(tmp_path):
    s,c,p,roots=setup(tmp_path)
    added=instrument(key='new',security_id='99')
    master(s,'b'*64,[instrument(),added]);schedule(c,CLOCK)
    first=s.document(KEY)[0]['families']['daily']['job_id']
    master(s,'c'*64,[added,instrument(),instrument(key='third',security_id='100')]);schedule(c,CLOCK)
    slot=s.document(KEY)[0]['families']['daily'];job=c.get_job(slot['job_id'])
    assert slot['added_identities']==1 and slot['job_id']!=first
    assert c.select_item(job)[1]['security_id']=='100'


def test_crash_after_job_before_checkpoint_is_idempotent(tmp_path,monkeypatch):
    import catalogue_backfill as module
    s,c,p,roots=setup(tmp_path)
    master(s,'b'*64,[instrument(),instrument(key='new',security_id='99')])
    original=module.replace
    def interrupted(store,key,fn):
        if key==KEY:raise RuntimeError('checkpoint storage unavailable')
        return original(store,key,fn)
    monkeypatch.setattr(module,'replace',interrupted)
    with pytest.raises(RuntimeError):schedule(c,CLOCK)
    after=s.document_names('jobs/');assert len(after)==4
    monkeypatch.setattr(module,'replace',original);schedule(c,CLOCK)
    assert len(s.document_names('jobs/'))==5 # Daily reused; intraday added once.
    schedule(c,CLOCK);assert len(s.document_names('jobs/'))==5


def test_missing_previous_manifest_stops_without_claiming_schedule(tmp_path):
    s,c,p,roots=setup(tmp_path)
    replace(s,'jobs/'+roots['daily'],lambda job:{**job,'catalogue_sha256':'f'*64})
    master(s,'b'*64,[instrument(),instrument(security_id='99')])
    with pytest.raises(ValueError,match='Previous pinned'):schedule(c,CLOCK)
    assert s.document(KEY)[0] is None and len(s.document_names('jobs/'))==3


def test_manual_pause_is_retained(tmp_path):
    s,c,p,roots=setup(tmp_path)
    replace(s,'jobs/'+roots['daily'],lambda job:{**job,'system_role':'backfill_daily'})
    c.control(roots['daily'],'pause')
    master(s,'b'*64,[instrument(),instrument(security_id='99')]);schedule(c,CLOCK)
    jid=s.document(KEY)[0]['families']['daily']['job_id']
    assert c.get_job(jid)['state']=='paused' and c.get_job(roots['daily'])['system_user_paused']


def test_rolling_compares_underlying_not_representative_option_strike(tmp_path):
    s,c,p,roots=setup(tmp_path)
    old=s.document('catalogue/'+'a'*64+'/manifest')[0]
    roll=instrument(key='r',segment='NSE_FNO',instrument='OPTIDX',expiry=None,expiry_flags=['MONTH'],strike='22000')
    k='catalogue/'+'a'*64+'/rolling';s.put_document(k,{'items':[roll]},0)
    replace(s,'catalogue/'+'a'*64+'/manifest',lambda cat:{**cat,'pages':cat['pages']+[{'group':'rolling-underlyings','key':k,'count':1}]})
    master(s,'b'*64,[instrument()],rolling=[{**roll,'key':'new-row','strike':'23000'},
        {**roll,'security_id':'99','symbol':'OTHER'}])
    schedule(c,CLOCK)
    slot=s.document(KEY)[0]['families']['rolling'];job=c.get_job(slot['job_id'])
    assert slot['added_identities']==1 and job['rolling_identity_version']==2
    assert c.select_item(job)[1]['security_id']=='99'
