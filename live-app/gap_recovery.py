"""Bounded, checkpointed recovery; provider rejections remain unresolved evidence."""
import time
from concurrent.futures import ThreadPoolExecutor
from collection import digest, now, replace

CONTROL='control/gap-recovery'

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
    # Do not repeat unchanged entitlement, parameter or no-data rejections.
    return result.get('http_status') in (500,502,503,504) or any('HTTP '+str(code) in reason for code in (500,502,503,504))

def tick(collector):
    store=collector.store;state=store.document(CONTROL)[0] or {}
    if not state.get('enabled') or state.get('next_scan_at',0)>time.time():return False
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
