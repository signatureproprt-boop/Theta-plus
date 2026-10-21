from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest
from fastapi import HTTPException
from collection import Collector,CollectionPlan,install_once
from store import Store
from research_archive import publish,chain_record
from research import saved_candles
from request_review import repair_verified_range
from test_collection import instrument,seed,Provider,candles


def test_collected_minute_index_evidence_reaches_chart_and_replay_excludes_future_receipt(tmp_path):
    s=Store(tmp_path/'s.db');c=Collector(Provider(),s)
    task={'family':'intraday','item':instrument(),'variant':{'interval':'1'},'from':'2026-10-16','to':'2026-10-17'}
    received=datetime.now(timezone.utc).isoformat();response={'raw':candles(),'received_at':received}
    result=c.persist_result(task,response);publish(s,task,response,result)
    clock=datetime.now(timezone.utc)+timedelta(seconds=5)
    view=saved_candles(s,'nifty',clock)
    assert len(view['candles'])==1 and view['candles'][0]['close']==11
    assert any(k.startswith('candles/') for k in view['source_record_ids'])
    assert not saved_candles(s,'nifty',datetime.fromisoformat(received)-timedelta(seconds=1))['candles']
    assert not saved_candles(s,'banknifty',clock)['candles']
    partition='candles/'+result['series']+'/2026-10-16';doc,v=s.document(partition)
    s.put_document(partition,{**doc,'updated_at':(clock+timedelta(seconds=1)).isoformat()},v)
    assert not saved_candles(s,'nifty',clock)['candles']


@pytest.mark.parametrize('family,interval,item',[('daily','1',instrument()),('intraday','5',instrument()),('rolling','1',instrument()),('intraday','1',instrument(security_id='99'))])
def test_daily_rolling_other_timeframes_and_wrong_identity_cannot_enter_minute_chart(tmp_path,family,interval,item):
    s=Store(tmp_path/'s.db')
    publish(s,{'family':family,'variant':{'interval':interval},'item':item},{'received_at':datetime.now(timezone.utc).isoformat()},{'rows':1,'days_saved':[]})
    assert s.document('research/collected/history/nifty')[0] is None


def test_collector_chain_uses_nearest_expiry_latest_receipt_without_freshness_upgrade(tmp_path):
    s=Store(tmp_path/'s.db');now=datetime.now(timezone.utc);expiry=(now+timedelta(days=7)).date().isoformat()
    task={'family':'chain_snapshot','item':instrument(),'variant':{},'expiry':expiry}
    response={'received_at':(now-timedelta(seconds=5)).isoformat(),'raw':{'data':{'last_price':100,'oc':{'100':{'ce':{'security_id':1}}}}}}
    publish(s,task,response,{'rows':1})
    selected=chain_record(s,'nifty',now)
    assert selected['metadata']['exchange_at'] is None
    assert selected['raw']==response['raw']
    future=deepcopy(response);future['received_at']=(now+timedelta(seconds=10)).isoformat()
    publish(s,task,future,{'rows':1})
    assert chain_record(s,'nifty',now) is None
    # A fresh legacy snapshot is still available when the archive is later than replay time.
    assert chain_record(s,'nifty',now,selected)==selected


def test_verified_range_repair_preserves_original_failed_outcome_and_checkpoint(tmp_path):
    s=Store(tmp_path/'s.db');seed(s);c=Collector(Provider(),s)
    jid=c.create(CollectionPlan(start='2026-10-01',end='2026-10-19',families=['daily']))['id']
    parent=c.get_job(jid);task=c.task(parent,'daily',instrument());key='results/'+jid+'/fixture'
    install_once(s,key,{'task':task,'result':{'status':'failed','rows':0}})
    report={'result_key':key,'can_repair':True,'original_task':task,'probes':[{'endpoint':'charts/historical','state':'ROWS_PRESENT','rows':5,'request':{'fromDate':'2026-10-12','toDate':'2026-10-19'}}]}
    install_once(s,'research/request-review/'+jid,report)
    child=repair_verified_range(c,jid,key);stored=c.get_job(child['id'])
    assert stored['current']['from']=='2026-10-12' and stored['current']['to']=='2026-10-19'
    assert stored['repair_scope']=='VERIFIED_WINDOW_ONLY_ORIGINAL_GAP_REMAINS'
    assert stored['state']=='paused' and c.get_job(jid)==parent
    assert s.document(key)[0]['result']['status']=='failed'
    with pytest.raises(HTTPException):repair_verified_range(c,jid,'results/'+jid+'/unverified')
    report['probes'][0]['request']['fromDate']='2025-01-01';s.put_document('research/request-review/'+jid,report,s.document('research/request-review/'+jid)[1])
    with pytest.raises(HTTPException):repair_verified_range(c,jid,key)


def test_existing_index_partitions_load_without_reindex_or_provider_calls(tmp_path):
    s=Store(tmp_path/'s.db');c=Collector(Provider(),s);clock=datetime.now(timezone.utc)+timedelta(seconds=2)
    day=(clock-timedelta(days=1)).date()
    while day.weekday()>4:day-=timedelta(days=1)
    task={'family':'intraday','item':instrument(),'variant':{'interval':'1'},'from':day.isoformat(),'to':(day+timedelta(days=1)).isoformat()}
    c.persist_result(task,{'raw':candles(day.isoformat()),'received_at':datetime.now(timezone.utc).isoformat()})
    assert s.document('research/collected/history/nifty')[0] is None
    assert len(saved_candles(s,'nifty',clock)['candles'])==1


def test_index_priority_plan_does_not_scan_unrelated_segment_shards(tmp_path):
    s=Store(tmp_path/'s.db');cat=seed(s);cat['pages'].append({'key':'catalogue/unused','group':'BSE_CURRENCY-FUTCUR','count':1000})
    s.put_document('catalogue/current',cat,s.document('catalogue/current')[1]);c=Collector(Provider(),s)
    j=c.create(CollectionPlan(start='2026-10-01',end='2026-10-19',families=['intraday'],segments=['IDX_I']))
    assert len(c.get_job(j['id'])['pages'])==1
    broad=c.create(CollectionPlan(start='2026-10-01',end='2026-10-19',families=['intraday']))
    assert len(c.get_job(broad['id'])['pages'])==2
