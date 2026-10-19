"""Bounded read-only diagnosis. Never advances a collector checkpoint."""
from datetime import datetime,timedelta,timezone
from fastapi import HTTPException
from dhan_data import candle_count
from provider import ProviderError
from runtime import IST

async def diagnose(collector,jid):
    job=collector.get_job(jid);task=job.get('current')
    if not task or job['state'] not in ('request_review','permission_blocked','token_required'):
        raise HTTPException(409,'No blocked current request to diagnose')
    item=task['item'];cat=collector.catalogue();matches=[]
    for page in (cat or {}).get('pages',[]):
        if page.get('group')!=item.get('segment','')+'-'+item.get('instrument',''):continue
        for row in (collector.store.document(page['key'])[0] or {}).get('items',[]):
            if str(row.get('security_id'))==str(item['security_id']) and row.get('segment')==item.get('segment') and row.get('instrument')==item.get('instrument'):matches.append(row)
    report={'job_id':jid,'checked_at':datetime.now(timezone.utc).isoformat(),'original_task':task,'request_error':job.get('request_error'),
            'master_matches':len(matches),'probes':[],'checkpoint_changed':False,
            'dh907_meaning':'Incorrect parameters OR no data present; this is not proof of missing subscription or complete data.',
            'source':'https://dhanhq.co/docs/v2/annexure/'}
    if len(matches)!=1:
        report['conclusion']='CURRENT_MASTER_IDENTITY_UNRESOLVED';return report
    if task['family'] not in ('daily','intraday'):
        report['conclusion']='MANUAL_ENDPOINT_REVIEW_REQUIRED';return report
    end=datetime.now(IST).date();start=end-timedelta(days=7)
    for endpoint in ('charts/historical','charts/intraday'):
        body={'securityId':str(item['security_id']),'exchangeSegment':item['segment'],'instrument':item['instrument'],'oi':False,
              'fromDate':start.isoformat(),'toDate':end.isoformat()}
        if endpoint.endswith('historical'):body['expiryCode']=0
        else:body.update(interval='1',fromDate=start.isoformat()+' 00:00:00',toDate=end.isoformat()+' 00:00:00')
        probe={'endpoint':endpoint,'request':body}
        try:
            raw=await collector.provider.post(endpoint,body);probe.update(state='ROWS_PRESENT' if candle_count(raw) else 'EMPTY',rows=candle_count(raw))
        except ProviderError as exc:probe.update(state='REJECTED',code=exc.code,reason=str(exc))
        report['probes'].append(probe)
        if probe.get('code') in ('DH-901','DH-902','807','808','809','810'):break
    report['conclusion']='RECENT_RANGE_WORKS_REVIEW_ORIGINAL_DATES' if report['probes'][0]['state']=='ROWS_PRESENT' else 'SOURCE_REQUEST_REMAINS_UNRESOLVED'
    if len(report['probes'])==2 and report['probes'][0]['state']=='REJECTED' and report['probes'][1]['state']=='ROWS_PRESENT':
        report['conclusion']='RECENT_INTRADAY_AVAILABLE_DAILY_REQUEST_REJECTED'
    from collection import replace
    replace(collector.store,'research/request-review/'+jid,lambda _:report)
    return report
