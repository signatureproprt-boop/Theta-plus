"""Bounded empirical check of an unresolved NSE commodity segment.

NSE_COMM is a candidate, not an installed catalogue mapping. Only provider
responses establish which tested data families work. No ID crosses exchanges.
"""
import asyncio
from datetime import date,datetime,timedelta,timezone
import time
import math
import anyio
from fastapi import HTTPException
from collection import now,replace
from collection_contracts import quote_values
from provider import ProviderError
from request_review import acquire_diagnostic
from runtime import IST

REPORT='research/nse-commodity-probe'

def public_report(store):
    report=store.document(REPORT)[0] or {}
    return {k:v for k,v in report.items() if k!='raw'} or {'state':'NOT_CHECKED','mapping_changed':False}

def select_samples(collector,cat):
    today=datetime.now(IST).date();selected={}
    for page in cat.get('pages',[]):
        if page.get('group') not in ('UNMAPPED-FUTCOM','UNMAPPED-OPTFUT'):continue
        for item in collector.page_items(page['key']):
            if item.get('exchange')!='NSE' or item.get('raw_segment')!='M' or item.get('segment') is not None:continue
            if item.get('reason')!='No documented API segment mapping: NSE_M':continue
            sid=item.get('security_id','')
            try:expiry=date.fromisoformat(item.get('expiry') or '')
            except (ValueError,TypeError):continue
            if not isinstance(sid,str) or not sid.isdigit() or int(sid)<=0 or expiry<today:continue
            kind=item.get('instrument')
            rank=(expiry,item.get('symbol',''),int(sid))
            if kind not in selected or rank<selected[kind][0]:selected[kind]=(rank,item)
    return [selected[k][1] for k in ('FUTCOM','OPTFUT') if k in selected]

async def check(collector):
    cat=await asyncio.to_thread(collector.catalogue)
    if not cat:return {'state':'CATALOGUE_UNAVAILABLE','mapping_changed':False}
    previous=await asyncio.to_thread(public_report,collector.store)
    if previous.get('catalogue_sha256')==cat['sha256'] and previous.get('checked_epoch',0)>time.time()-600:
        return {**previous,'cached':True}
    try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(cat['fetched_at'])).total_seconds()
    except (KeyError,TypeError,ValueError):age=float('inf')
    if not -5<=age<=86400:return {'state':'CATALOGUE_STALE','mapping_changed':False}
    samples=await asyncio.to_thread(select_samples,collector,cat)
    report={'state':'NOT_CHECKED','mapping_changed':False,'candidate_segment':'NSE_COMM',
            'catalogue_sha256':cat['sha256'],'checked_at':now(),'checked_epoch':time.time(),
            'samples':samples,'probes':[],
            'scope':'Only the returned sample identities and tested REST data families; WebSocket enum and full-universe coverage remain unverified.'}
    if not samples:report['state']='NO_CURRENT_SAMPLE'
    else:
        try:owner=await acquire_diagnostic(collector)
        except HTTPException as error:
            # A busy diagnostic does not invalidate the separately checked token.
            return {**report,'state':'COLLECTOR_BUSY' if error.status_code==409 else 'PROBE_UNAVAILABLE'}
        try:
            ids=[int(i['security_id']) for i in samples]
            probe={'endpoint':'marketfeed/quote','request':{'NSE_COMM':ids}}
            try:
                raw=await collector.provider.probe_commodity_quotes(ids)
                values=quote_values({'raw':raw},'NSE_COMM')
                observed=[]
                for sid in ids:
                    value=values.get(str(sid)) or {}
                    price=value.get('last_price') if isinstance(value,dict) else None
                    if isinstance(price,(int,float)) and not isinstance(price,bool) and math.isfinite(price) and price>0:
                        observed.append(str(sid))
                probe.update(state='QUOTE_RETURNED' if observed else 'NO_MATCHING_QUOTE',observed_security_ids=observed)
                report['raw']={'quote':raw};report['state']=probe['state']
            except ProviderError as error:
                probe.update(state='PROVIDER_REJECTED',code=error.code,http_status=error.status,reason=str(error))
                report['state']='PROVIDER_REJECTED'
            report['probes'].append(probe)
            # A returned quote does not establish historical or streaming support.
            future=next((i for i in samples if i['instrument']=='FUTCOM' and
                         str(i['security_id']) in probe.get('observed_security_ids',[])),None)
            if future:
                end=datetime.now(IST).date();start=end-timedelta(days=7)
                body={'securityId':future['security_id'],'exchangeSegment':'NSE_COMM','instrument':'FUTCOM',
                      'expiryCode':0,'oi':True,'fromDate':start.isoformat(),'toDate':end.isoformat()}
                historical={'endpoint':'charts/historical','request':body}
                try:
                    raw=await collector.call(owner,'charts/historical',body)
                    from dhan_data import candle_count
                    rows=candle_count(raw);historical.update(state='ROWS_RETURNED' if rows else 'EMPTY',rows=rows)
                    report.setdefault('raw',{})['daily']=raw
                except ProviderError as error:
                    historical.update(state='PROVIDER_REJECTED' if error.code else 'INVALID_RESPONSE',code=error.code,http_status=error.status,reason=str(error))
                report['probes'].append(historical)
        finally:
            with anyio.CancelScope(shield=True):await asyncio.to_thread(collector.release,owner)
    await asyncio.to_thread(collector.store.add,'segment_probe_source',report)
    await asyncio.to_thread(replace,collector.store,REPORT,lambda _:report)
    return {k:v for k,v in report.items() if k!='raw'}
