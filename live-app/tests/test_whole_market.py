from collections import defaultdict
from copy import deepcopy
from datetime import datetime,timezone
from collection import Collector,CollectionPlan,replace
from store import Store
from test_collection import instrument,Provider
from whole_market import refresh,EARLIER,KEY

CLOCK=datetime(2026,10,24,8,tzinfo=timezone.utc)

def master(c,sha,items):
    groups=defaultdict(list)
    for item in items:groups[(item.get('segment') or 'UNMAPPED')+'-'+item['instrument']].append(item)
    pages=[]
    for n,(group,entries) in enumerate(groups.items()):
        key=f'catalogue/{sha}/pages/{n}'
        c.store.put_document(key,{'items':entries},0)
        pages.append({'key':key,'group':group,'count':len(entries)})
    cat={'sha256':sha,'rows':len(items),'pages':pages,'fetched_at':CLOCK.isoformat()}
    c.store.put_document('catalogue/'+sha+'/manifest',cat,0)
    replace(c.store,'catalogue/current',lambda _:cat)
    return cat

def setup(tmp_path):
    c=Collector(Provider(),Store(tmp_path/'db'))
    items=[instrument(),instrument(key='cash',segment='NSE_EQ',raw_segment='E',instrument='EQUITY',security_id='99'),
        instrument(key='bse',exchange='BSE',segment='BSE_EQ',raw_segment='E',instrument='EQUITY',security_id='100'),
        instrument(key='commodity',exchange='MCX',segment='MCX_COMM',raw_segment='M',instrument='FUTCOM',security_id='101'),
        instrument(key='unmapped',segment=None,raw_segment='M',instrument='OPTFUT',security_id='102')]
    master(c,'a'*64,items);roots={}
    for family in ('daily','intraday','rolling'):
        job=c.create(CollectionPlan(start='2021-10-19',end='2026-10-19',families=[family],intervals=[1]))
        c.control(job['id'],'resume');roots[family]=job['id']
    c.store.put_document('system/backfill',{'jobs':roots},0)
    return c,roots,items

def test_all_segments_visible_and_earlier_cash_daily_is_idempotent(tmp_path):
    c,roots,items=setup(tmp_path);before={f:deepcopy(c.get_job(j)) for f,j in roots.items()}
    report=refresh(c,CLOCK)
    assert report['catalogue_rows']==5 and report['mapped_rows']==4 and report['unmapped_rows']==1
    assert len(report['segments'])==5 and report['coverage_verified'] is False
    jid=report['earlier_daily']['latest_job_id'];job=c.get_job(jid)
    assert job['state']=='running' and job['system_role']=='all_daily_earlier'
    assert job['config']['start']=='1900-01-01' and job['config']['end']=='2021-10-19'
    assert job['config']['families']==['daily'] and job['config']['symbols']==[]
    assert [p['group'] for p in job['pages']]==['IDX_I-INDEX','NSE_EQ-EQUITY','BSE_EQ-EQUITY']
    assert all(c.get_job(j)==before[f] for f,j in roots.items())
    ids=c.store.document_names('jobs/');refresh(c,CLOCK)
    assert c.store.document_names('jobs/')==ids
    assert c.store.document(KEY)[0]['coverage_verified'] is False

def test_new_master_only_schedules_new_cash_identity(tmp_path):
    c,roots,items=setup(tmp_path);first=refresh(c,CLOCK)['earlier_daily']['latest_job_id'];before=deepcopy(c.get_job(first))
    master(c,'b'*64,items+[instrument(key='new',segment='NSE_EQ',instrument='EQUITY',security_id='888'),
        instrument(key='newcommodity',exchange='MCX',segment='MCX_COMM',instrument='FUTCOM',security_id='889')])
    report=refresh(c,CLOCK);second=report['earlier_daily']['latest_job_id']
    assert second!=first and c.get_job(first)==before
    job=c.get_job(second);assert len(job['pages'])==1 and job['pages'][0]['count']==1
    assert c.select_item(job)[1]['security_id']=='888'
    assert report['earlier_daily']['job_ids']==[first,second]

def test_manual_pause_and_missing_manifest_are_never_reset(tmp_path):
    c,roots,items=setup(tmp_path);first=refresh(c,CLOCK)['earlier_daily']['latest_job_id']
    c.control(first,'pause');master(c,'b'*64,items+[instrument(key='new',security_id='888')])
    report=refresh(c,CLOCK)
    assert report['earlier_daily']['state']=='EARLIER_MANUALLY_PAUSED' and len(c.store.document_names('jobs/'))==4
    c.control(first,'resume')
    replace(c.store,EARLIER,lambda s:{**s,'catalogue_sha256':'f'*64})
    report=refresh(c,CLOCK)
    assert report['earlier_daily']['state']=='EARLIER_DAILY_REVIEW_REQUIRED'
    assert report['read_issues'][0]['error_type']=='ValueError' and len(c.store.document_names('jobs/'))==4

def test_source_manual_pause_prevents_new_earlier_job(tmp_path):
    c,roots,_=setup(tmp_path)
    replace(c.store,'jobs/'+roots['daily'],lambda j:{**j,'system_role':'backfill_daily'})
    c.control(roots['daily'],'pause');report=refresh(c,CLOCK)
    assert report['earlier_daily']['state']=='SOURCE_MANUALLY_PAUSED'
    assert len(c.store.document_names('jobs/'))==3
