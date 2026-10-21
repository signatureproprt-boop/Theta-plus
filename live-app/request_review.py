"""Bounded read-only diagnosis. Never advances a collector checkpoint."""
import asyncio
from datetime import date,datetime,timedelta,timezone
from fastapi import HTTPException
from dhan_data import candle_count
from provider import ProviderError
from runtime import IST

async def diagnose(collector,jid,result_key=None):
    job=collector.get_job(jid);task=job.get('current')
    if result_key:
        if not result_key.startswith('results/'+jid+'/'):raise HTTPException(422,'Choose a result from this job')
        entry=collector.store.document(result_key)[0]
        if not entry or entry.get('result',{}).get('status') not in ('failed','partial','empty','permission_blocked'):
            raise HTTPException(422,'Choose an unresolved result')
        task=entry.get('task')
    if not task or (not result_key and job['state'] not in ('request_review','permission_blocked','token_required')):
        raise HTTPException(409,'No blocked current request to diagnose')
    owner=await asyncio.to_thread(collector.acquire)
    try:return await _diagnose(collector,jid,job,task,owner,result_key)
    finally:await asyncio.to_thread(collector.release,owner)

async def _diagnose(collector,jid,job,task,owner,result_key):
    item=task['item'];cat=collector.catalogue();matches=[]
    for page in (cat or {}).get('pages',[]):
        if page.get('group')!=item.get('segment','')+'-'+item.get('instrument',''):continue
        for row in (collector.store.document(page['key'])[0] or {}).get('items',[]):
            if str(row.get('security_id'))==str(item['security_id']) and row.get('segment')==item.get('segment') and row.get('instrument')==item.get('instrument'):matches.append(row)
    report={'job_id':jid,'result_key':result_key,'checked_at':datetime.now(timezone.utc).isoformat(),'original_task':task,'request_error':job.get('request_error'),
            'master_matches':len(matches),'probes':[],'checkpoint_changed':False,
            'dh907_meaning':'Incorrect parameters OR no data present; this is not proof of missing subscription or complete data.',
            'source':'https://dhanhq.co/docs/v2/annexure/'}
    if len(matches)!=1:
        report['conclusion']='CURRENT_MASTER_IDENTITY_UNRESOLVED';return report
    if task['family']=='quotes':
        body={item['segment']:[int(item['security_id'])]};probe={'endpoint':'marketfeed/quote','request':body}
        try:
            raw=await collector.call(owner,'marketfeed/quote',body)
            values=(raw.get('data') or {}).get(item['segment'],{})
            value=values.get(item['security_id']) if isinstance(values,dict) else None
            probe.update(state='ROWS_PRESENT' if isinstance(value,dict) and value else 'EMPTY',rows=int(isinstance(value,dict) and bool(value)))
        except ProviderError as exc:probe.update(state='REJECTED',code=exc.code,reason=str(exc))
        report['probes'].append(probe)
        report['conclusion']='ISOLATED_QUOTE_RETURNED_REVIEW_BULK_BATCH' if probe['state']=='ROWS_PRESENT' else 'ISOLATED_QUOTE_UNRESOLVED'
        from collection import replace
        replace(collector.store,'research/request-review/'+jid,lambda _:report)
        return report
    if task['family'] not in ('daily','intraday'):
        report['conclusion']='MANUAL_ENDPOINT_REVIEW_REQUIRED';return report
    end=min(datetime.now(IST).date(),date.fromisoformat(task['to']))
    if item.get('expiry'):
        end=min(end,date.fromisoformat(item['expiry'])+timedelta(days=1))
    start=max(date.fromisoformat(task['from']),end-timedelta(days=7))
    if start>=end:
        report['conclusion']='NO_VALID_BOUNDED_HISTORY_WINDOW';return report
    for endpoint in ('charts/historical','charts/intraday'):
        body={'securityId':str(item['security_id']),'exchangeSegment':item['segment'],'instrument':item['instrument'],'oi':False,
              'fromDate':start.isoformat(),'toDate':end.isoformat()}
        if endpoint.endswith('historical'):body['expiryCode']=0
        else:body.update(interval='1',fromDate=start.isoformat()+' 00:00:00',toDate=end.isoformat()+' 00:00:00')
        probe={'endpoint':endpoint,'request':body}
        try:
            raw=await collector.call(owner,endpoint,body);probe.update(state='ROWS_PRESENT' if candle_count(raw) else 'EMPTY',rows=candle_count(raw))
        except ProviderError as exc:probe.update(state='REJECTED',code=exc.code,reason=str(exc))
        report['probes'].append(probe)
        if probe.get('code') in ('DH-901','DH-902','DH-904','805','806','807','808','809','810','BUDGET'):break
    report['can_repair']=any(p['state']=='ROWS_PRESENT' and p['endpoint']==('charts/historical' if task['family']=='daily' else 'charts/intraday') for p in report['probes'])
    report['conclusion']='RECENT_RANGE_WORKS_REVIEW_ORIGINAL_DATES' if report['probes'][0]['state']=='ROWS_PRESENT' else 'SOURCE_REQUEST_REMAINS_UNRESOLVED'
    if len(report['probes'])==2 and report['probes'][0]['state']=='REJECTED' and report['probes'][1]['state']=='ROWS_PRESENT':
        report['conclusion']='RECENT_INTRADAY_AVAILABLE_DAILY_REQUEST_REJECTED'
    from collection import replace
    replace(collector.store,'research/request-review/'+jid,lambda _:report)
    return report


def repair_verified_range(collector,jid,result_key):
    """Only a provider-observed window becomes a separate, explicit retry plan."""
    report=collector.store.document('research/request-review/'+jid)[0] or {}
    if not result_key or report.get('result_key')!=result_key or not report.get('can_repair'):
        raise HTTPException(409,'Diagnose this result and verify returned history before preparing a repair')
    task=report['original_task'];family=task['family']
    endpoint='charts/historical' if family=='daily' else 'charts/intraday'
    probe=next((p for p in report['probes'] if p['endpoint']==endpoint and p['state']=='ROWS_PRESENT'),None)
    if not probe:raise HTTPException(409,'No verified window for this data family')
    start=probe['request']['fromDate'][:10];end=probe['request']['toDate'][:10]
    if not task['from']<=start<end<=task['to']:raise HTTPException(409,'Diagnostic range is outside the original request')
    child=collector.retry(jid,result_key)
    from collection import replace
    def bounded(job):
        job['current']={**job['current'],'from':start,'to':end}
        job['config']={**job['config'],'start':start,'end':end,'families':[family],
                       'symbols':[task['item']['symbol']],'segments':[task['item']['segment']]}
        job['repair_scope']='VERIFIED_WINDOW_ONLY_ORIGINAL_GAP_REMAINS'
        job['message']='Verified-window retry ready. Original failed range and outcome remain unchanged. Resume to save only this bounded range.'
        job.pop('request_error',None)
        return job
    return collector.summary(replace(collector.store,'jobs/'+child['id'],bounded))
