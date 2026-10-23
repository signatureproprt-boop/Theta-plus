"""Bounded, checkpointed recovery; provider rejections remain unresolved evidence."""
import time
from datetime import date,datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
from collection import digest, now, replace
from runtime import IST

CONTROL='control/gap-recovery'
SCAN_POLICY='validated-dh905-once-v1'

def valid_history_retry(task):
    """Admit one unchanged DH-905 retry only for a valid history wire shape.

    A production request with identical security ID, segment and dates returned
    candles after its first DH-905 rejection. This does not make all parameter
    or no-data errors transient; tick still retains the original and permits
    only one child per result.
    """
    family=task.get('family');item=task.get('item') or {}
    if family not in ('daily','intraday') or task.get('skip') or item.get('reason'):return False
    instruments={'IDX_I':{'INDEX'},'NSE_EQ':{'EQUITY'},'BSE_EQ':{'EQUITY'},
                 'NSE_FNO':{'FUTIDX','FUTSTK','OPTIDX','OPTSTK'},
                 'BSE_FNO':{'FUTIDX','FUTSTK','OPTIDX','OPTSTK'},
                 'MCX_COMM':{'FUTCOM','OPTFUT'},
                 'NSE_CURRENCY':{'FUTCUR','OPTCUR'},'BSE_CURRENCY':{'FUTCUR','OPTCUR'}}
    sid=item.get('security_id')
    if not isinstance(sid,str) or not sid.isdigit() or int(sid)<=0:return False
    if item.get('instrument') not in instruments.get(item.get('segment'),set()):return False
    try:
        start=date.fromisoformat(task['from']);end=date.fromisoformat(task['to'])
        today=datetime.now(IST).date()
        if not date(1900,1,1)<=start<end<=today:return False
        variant=task.get('variant')
        if family=='daily':return variant=={}
        if (not isinstance(variant,dict) or set(variant)!={'interval'} or
                str(variant['interval']) not in ('1','5','15','25','60')):return False
        if (end-start).days>90 or start<today-timedelta(days=5*365+2):return False
        if item.get('expiry') and date.fromisoformat(item['expiry'])<today:return False
        return True
    except (KeyError,ValueError,TypeError):return False

def all_jobs(store):
    cursor=''
    while True:
        names=store.document_names('jobs/',cursor,100)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for data in pool.map(store.document,names):
                if data[0]:yield data[0]
        if len(names)<100:return
        cursor=names[-1]

def eligible(entry):
    result=entry['result'];family=entry['task']['family']
    if result.get('status')=='partial' and family in ('daily','intraday','rolling'):
        # One deterministic retry via tick; an unchanged empty/provider rejection
        # remains visible. Whole absent days count, not only holes between rows.
        return bool(result.get('calendar_verified') and (result.get('missing_sessions') or result.get('missing_minutes')))
    if result.get('status')=='partial' and result.get('missing_security_ids'):
        return family in ('quotes','live','depth20','depth200')
    if result.get('status')!='failed':return False
    if family in ('live','depth20','depth200') and result.get('decode_failed_observations'):return True
    reason=result.get('reason') or ''
    if family in ('daily','intraday','rolling') and 'Invalid OHLC or volume' in reason:return True
    if result.get('code')=='DH-905' and result.get('http_status')==400:
        return valid_history_retry(entry['task'])
    # Entitlement, invalid wire shapes and ambiguous DH-907 no-data rejections
    # still need diagnosis, not an automatic retry loop.
    return result.get('http_status') in (500,502,503,504) or any('HTTP '+str(code) in reason for code in (500,502,503,504))

def tick(collector):
    store=collector.store;state=store.document(CONTROL)[0] or {}
    if not state.get('enabled'):return False
    if state.get('scan_policy')!=SCAN_POLICY:
        # Revisit previously skipped DH-905 outcomes once under the new policy.
        # Deterministic child IDs and retry_of checks prevent duplicate retries.
        def upgrade(old):
            if old.get('scan_policy')==SCAN_POLICY:return old
            return {**old,'scan_policy':SCAN_POLICY,'position':0,'cursors':{},'next_scan_at':0}
        state=replace(store,CONTROL,upgrade)
    if state.get('next_scan_at',0)>time.time():return False
    jobs=list(all_jobs(store))
    sources=sorted((j for j in jobs if not j.get('retry_of') and j['state'] in ('running','completed')),key=lambda j:j['id'])
    if not sources:return False
    position=state.get('position',0)%len(sources);parent=sources[position];jid=parent['id']
    cursors=dict(state.get('cursors',{}));page=collector.results(jid,cursors.get(jid,''))
    queued=0;processed=0;last=None
    for entry in page['items']:
        processed+=1;last=entry['key']
        if not eligible(entry):continue
        task=entry['task']
        if task['family']=='rolling' and (task.get('variant',{}).get('expiryCode') not in (1,2,3) or
                (task['item']['instrument']=='OPTIDX' and parent.get('rolling_identity_version')!=2)):
            continue
        previous=[j for j in jobs if j.get('retry_of')==entry['key']]
        quarantine='Invalid OHLC or volume' in (entry['result'].get('reason') or '')
        if previous and (not quarantine or any(j.get('rows_saved') or j['config'].get('quarantine_invalid_candles') for j in previous)):
            continue
        # Deterministic identity closes a crash between creation and checkpoint.
        child_id=digest(['gap-recovery-v1',entry['key']])[:32]
        child=collector.retry(jid,entry['key'],quarantine_invalid=quarantine,child_id=child_id)
        if child['state']=='paused':collector.control(child_id,'resume')
        queued+=1
        if queued>=5:break
    remaining=processed<len(page['items']) or bool(page.get('next_cursor'))
    if remaining and last:cursors[jid]=last
    else:cursors.pop(jid,None)
    next_position=(position+1)%len(sources)
    finished_round=next_position==0 and not cursors
    new={**state,'position':next_position,'cursors':cursors,'last_scan_at':now(),'last_source':jid,
         'inspected_total':state.get('inspected_total',0)+processed,
         'queued_total':state.get('queued_total',0)+queued,
         'next_scan_at':time.time()+900 if finished_round else 0,
         'status':'Scan finished; unresolved outcomes remain visible' if finished_round else 'Scanning saved outcomes'}
    replace(store,CONTROL,lambda _:new)
    return not finished_round or bool(queued)
