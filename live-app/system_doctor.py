"""Durable, resumable system diagnosis. No credentials, data or gaps are invented.

A registered check may pass only its stated evidence scope. Inventory traversal
is live, not a transactionally frozen snapshot; concurrent inserts before a
cursor and provider-unavailable history are never certified by this scan.
"""
import asyncio
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid
from fastapi import HTTPException
from collection import replace
from diagnostics import fingerprint
from runtime import IST

KEY='doctor/current'
ROOT=Path(__file__).parent
STATES={'VERIFIED','ISSUE','NOT_VERIFIED','UNAVAILABLE','REPAIRING'}
AREAS=('application','security','credentials','catalogue','live','history','worker','storage','daily','consistency')
NAMESPACES=('documents','records','journal')
LIMITS={'live':5000,'live-extra':5000,'live-3':5000,'live-4':5000,'depth20':50,'depth200':1}


def now():return datetime.now(timezone.utc)
def iso():return now().isoformat()
def stamp(value):
    try:return datetime.fromisoformat(value).timestamp()
    except (ValueError,TypeError):return 0

def check(area,key,state,evidence,next_action=''):
    if state not in STATES:raise ValueError('Unknown diagnostic state')
    return {'area':area,'id':key,'state':state,'evidence':evidence,'next_action':next_action,'checked_at':iso()}

def pending(store):return (store.document(KEY)[0] or {}).get('state') in ('QUEUED','SCANNING','REPAIRING','VERIFYING')

def start(store,repair=False,inventory=None,probes=None):
    scan_id=uuid.uuid4().hex
    def create(old):
        if old and old.get('state') in ('QUEUED','SCANNING','REPAIRING','VERIFYING'):
            raise HTTPException(409,'A scan is already pending. Refresh its progress or stop it first.')
        return {'id':scan_id,'state':'QUEUED','started_at':iso(),'repair_requested':repair,
                'phase':'checks','checks':[],'api_inventory':inventory or [],'http_probes':probes or [],
                'namespace':0,'cursor':'','inspected':{},'findings':{},'examples':[],
                'repairs':[],'scan_errors':0,'areas':list(AREAS),
                'coverage_verified':False,'scope':'All collection JSON documents, saved records and journal entries encountered by paginated traversal; application and data checks listed individually.',
                'limitations':['Live inventory is not an atomic snapshot; concurrent inserts before the cursor need another scan.',
                    'Binary source CSV archives, bucket IAM/billing, hard-kill recovery and every browser interaction require separate verification.',
                    'Historical outcome totals are attempts, not unique candle/date coverage. Lost ticks cannot be reconstructed.']}
    return replace(store,KEY,create)

def update(store,scan_id,operation):
    def write(old):
        if not old or old.get('id')!=scan_id or old.get('state')=='STOPPED':return old
        return {**operation(old),'updated_at':iso()}
    return replace(store,KEY,write)

def report(store):
    state=store.document(KEY)[0] or {'state':'NOT_STARTED','areas':list(AREAS),'checks':[],'coverage_verified':False}
    counts=dict(Counter(c['state'] for c in state.get('checks',[])))
    assessed={c['area'] for c in state.get('checks',[])}
    return {**state,'summary':counts,'unchecked_areas':[a for a in AREAS if a not in assessed],
            'completion':'No full-system or full-history certification. Review issues, unreadable records and NOT_VERIFIED checks.'}

def finding_pages(store,after=''):
    scan=store.document(KEY)[0] or {}
    if not scan.get('id'):return {'pages':[],'next_cursor':None}
    prefix='doctor/findings/'+scan['id']+'/'
    if after and not after.startswith(prefix):raise HTTPException(422,'Findings cursor belongs to another scan')
    names=store.document_names(prefix,after,11)
    return {'scan_id':scan['id'],'pages':[store.document(key)[0] for key in names[:10]],
            'next_cursor':names[9] if len(names)>10 else None}


def app_inventory(app):
    return [{'path':r.path,'methods':sorted(r.methods),'functional_verification':'NOT_VERIFIED'} for r in app.routes if getattr(r,'methods',None)]

def admin_configuration_check(context):
    # Admin credentials intentionally belong only to the web service. The
    # collector's restricted identity must not receive that secret to diagnose it.
    if context!='web_service':
        return check('security','admin_configuration','NOT_VERIFIED',
            {'execution_context':context,'scope':'Web-only Admin configuration is not available in the collector'},
            'Use web-service authentication probes; do not copy Admin secrets into the worker.')
    import os
    parts=os.environ.get('THETA_TOKEN_ADMIN_HASH','').split('$')
    valid=(len(parts)==4 and parts[:2]==['pbkdf2_sha256','600000'] and bool(re.fullmatch(r'[a-fA-F0-9]{16,128}',parts[2])) and bool(re.fullmatch(r'[a-fA-F0-9]{64}',parts[3])))
    return check('security','admin_configuration','VERIFIED' if valid else 'ISSUE',
        {'execution_context':context,'configured_hash_format_valid':valid,'scope':'Web-service format only; unauthorized request probes are separate'},
        'Configure Admin through the existing owner-controlled flow' if not valid else '')

async def application_probes(app):
    """Real in-process middleware/routing tests; never call a write with Admin auth."""
    import httpx,os
    host=os.environ.get('THETA_ALLOWED_HOSTS','localhost').split(',')[0].strip()
    if not re.fullmatch(r'[a-zA-Z0-9.-]+',host):return [check('application','http_probe','NOT_VERIFIED',{'reason':'No valid allowed host'})]
    headers={}
    if os.environ.get('THETA_AUTH_MODE')!='public' and os.environ.get('THETA_API_KEY'):
        headers['Authorization']='Bearer '+os.environ['THETA_API_KEY']
    results=[admin_configuration_check('web_service')]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host,headers=headers,timeout=15) as client:
        for method,path in [('GET','/health-check'),('POST','/api/admin/check'),
                            ('POST','/api/data-system/start'),('POST','/api/collection/catalogue/refresh'),
                            ('POST','/api/system-doctor/start')]:
            try:
                response=await client.request(method,path,**({'json':{}} if method=='POST' else {}))
                ok=response.status_code==200 if method=='GET' else response.status_code in (403,503)
                if method=='GET':
                    body=response.json();ok=ok and body.get('status')=='ok' and body.get('execution')=='NO_BROKER_EXECUTION'
                results.append(check('application' if method=='GET' else 'security','http:'+path,
                    'VERIFIED' if ok else 'ISSUE',{'method':method,'status':response.status_code,'scope':'Health response' if method=='GET' else 'Missing Admin credentials rejected'}))
            except Exception as error:results.append(check('application','http:'+path,'NOT_VERIFIED',fingerprint('doctor_http',error)))
        try:
            r=await client.get('/api/status',headers={'Origin':'https://doctor.invalid'})
            results.append(check('security','cross_origin','VERIFIED' if r.status_code==403 else 'ISSUE',{'status':r.status_code}))
        except Exception as error:results.append(check('security','cross_origin','NOT_VERIFIED',fingerprint('doctor_http',error)))
    return results


def system_checks(collector,scan):
    """Each component fails independently; raw exception strings never enter reports."""
    store=collector.store;checks=[]
    def run(area,key,fn):
        try:
            state,evidence,action=fn();checks.append(check(area,key,state,evidence,action))
        except Exception as error:checks.append(check(area,key,'ISSUE' if area=='credentials' and isinstance(error,HTTPException) and error.status_code in (422,503) else 'NOT_VERIFIED',fingerprint('doctor_check',error),'Review diagnostic code location; other checks continue.'))
    def static_assets():
        from html.parser import HTMLParser
        class Assets(HTMLParser):
            def __init__(self):super().__init__();self.paths=[];self.pages=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if attrs.get('data-page'):self.pages.append(attrs['data-page'])
                for attribute in ('src','href'):
                    value=attrs.get(attribute,'')
                    if value.startswith('/static/'):self.paths.append(value.split('?')[0])
        parser=Assets();parser.feed((ROOT/'static/index.html').read_text())
        missing=[p for p in parser.paths if not (ROOT/p.lstrip('/')).is_file()]
        return ('ISSUE' if missing else 'VERIFIED',{'assets':len(parser.paths),'missing':missing,'pages':parser.pages,'scope':'Referenced assets exist; browser behavior is separate'},'Repair missing assets' if missing else '')
    run('application','static_assets',static_assets)
    def source_modules():
        import ast
        modules=sorted(ROOT.glob('*.py'));bad=[]
        for path in modules:
            try:ast.parse(path.read_text(),filename=path.name)
            except (SyntaxError,UnicodeError):bad.append(path.name)
        return ('ISSUE' if bad else 'VERIFIED',{'python_modules':len(modules),'syntax_errors':bad,'scope':'All top-level runtime Python modules parse; behavior is not established by syntax'},'')
    run('application','runtime_source_inventory',source_modules)
    checks.extend(scan.get('http_probes',[]))
    checks.append(check('application','all_api_routes','NOT_VERIFIED',{'registered_routes':len(scan.get('api_inventory',[]))},'Route inventory is complete; parameter-specific reads and write workflows need separate live tests.'))
    checks.append(check('application','browser_workflows','NOT_VERIFIED',{'scope':'Buttons, forms, navigation, exports and screen/saved-data equality'},'Run browser interactions; static assets alone do not verify behavior.'))
    if not any(c.get('id')=='admin_configuration' for c in checks):
        checks.append(admin_configuration_check('collector_worker'))
    def provider_auth():
        from token_check import check_token
        credentials=collector.provider.credentials
        result=asyncio.run(check_token(credentials.token(),credentials.client_id))
        return ('VERIFIED',{'profile_authenticated':result.get('status')=='AUTHENTICATED','checked_at':iso(),
            'market_data_verified':False},'Each market-data endpoint still needs data evidence')
    run('credentials','saved_dhan_auth',provider_auth)
    def sdk():
        from provider import DATA_PATHS,SDK_VERSION
        unexpected=[p for p in DATA_PATHS if p not in {'charts/historical','charts/intraday','charts/rollingoption','optionchain','optionchain/expirylist','marketfeed/ltp','marketfeed/ohlc','marketfeed/quote'}]
        return ('ISSUE' if unexpected else 'VERIFIED',{'sdk_version':SDK_VERSION,'data_endpoints':sorted(DATA_PATHS),'unexpected':unexpected,'scope':'Installed SDK identity and data-only adapter allowlist'},'')
    run('security','provider_endpoint_allowlist',sdk)
    def catalogue():
        cat=collector.catalogue() or {};age=time.time()-stamp(cat.get('fetched_at'))
        reasons=cat.get('review_reasons',{})
        return ('ISSUE' if not cat or age>86400 or reasons else 'VERIFIED',{'rows':cat.get('rows',0),'age_seconds':round(age),
            'unresolved_reason_counts':reasons,'scope':'Current master mapping and age'},'Refresh stale master; resolve mappings only from official identities' if not cat or age>86400 or reasons else '')
    run('catalogue','instrument_master',catalogue)
    snapshot={}
    def lifecycle():
        from data_system import status
        snapshot.update(status(store))
        return ('VERIFIED' if snapshot.get('worker_active') else 'ISSUE',{'active':snapshot.get('worker_active'),'heartbeat':snapshot.get('worker_heartbeat'),'enabled':snapshot.get('enabled')},'Start worker when authorized collection is enabled; retain leases')
    run('worker','heartbeat',lifecycle)
    for family,limit in LIMITS.items():
        def stream(family=family,limit=limit):
            r=snapshot.get('streams',{}).get(family,{})
            evidence={k:r.get(k) for k in ('state','subscribed','observed_since_connection','missing_since_connection','decode_errors','last_durable_received_at','durable_receipt_age_seconds')}
            count=snapshot.get('subscribed',{}).get(family,0);evidence['configured_instruments']=count;evidence['capacity']=limit
            state='ISSUE' if count>limit or r.get('decode_errors',0) else 'VERIFIED' if r.get('state')=='RECORDING' and count and r.get('observed_since_connection',0)>0 else 'NOT_VERIFIED'
            return state,evidence,'Verify actual frames during the applicable market session; no freshness inferred from connection alone'
        run('live',family,stream)
    run('consistency','subscription_receipts',lambda:(
        'ISSUE' if any(r.get('subscribed') not in (None,snapshot.get('subscribed',{}).get(f)) for f,r in snapshot.get('streams',{}).items()) else 'VERIFIED',
        {'scope':'Recorder subscribed counts vs published manifest; exact binary identity validation occurs during archive scan'},''))
    def daily():
        m=snapshot.get('maintenance',{});history=m.get('history',{})
        evidence={'state':m.get('state'),'heartbeat':m.get('heartbeat'),'target':m.get('closed_day_target'),
            'families':{f:{k:v.get(k) for k in ('state','start','end','counts','verified_through')} for f,v in history.items()}}
        return ('NOT_VERIFIED' if not history else 'ISSUE' if m.get('state')=='DEGRADED' else 'NOT_VERIFIED',evidence,'Job completion is not candle/date coverage; inspect all archived outcomes')
    run('daily','catchup_coverage',daily)
    run('worker','scheduler_restart',lambda:('NOT_VERIFIED',{'reported_configured':bool(snapshot.get('watchdog',{}).get('reported_configured'))},'Private Scheduler and hard-kill restart need a cloud acceptance test; configuration alone does not pass'))
    run('history','unique_coverage',lambda:('NOT_VERIFIED',{'scope':'Every instrument/date/timeframe, including absent sessions'},'Stored-candle grids and outcomes are inspected below; absent entire series and unsupported calendars remain unverified'))
    for symbol in ('nifty','banknifty'):
        def workspace_check(symbol=symbol):
            from research import workspace
            data=workspace(store,symbol)
            valid=(data.get('symbol')==symbol and data.get('mode')=='READ_ONLY_RESEARCH' and isinstance(data.get('history',{}).get('candles'),list) and isinstance(data.get('chain',{}).get('contracts'),list))
            return ('VERIFIED' if valid else 'ISSUE',{'scope':'Saved workspace renderer schema; browser equality and quote freshness remain separate','symbol':symbol,'schema_valid':valid},'')
        run('consistency','saved_workspace:'+symbol,workspace_check)
    for family in ('quotes','chain'):
        run('history',family+'_freshness',lambda family=family:('NOT_VERIFIED',{'scope':family+' response schemas are checked in the archive traversal'},'A successful stored response does not establish current exchange freshness or continuous history'))
    def storage_probe():
        nonce=uuid.uuid4().hex;key='doctor/probe/'+scan['id']
        replace(store,key,lambda _: {'nonce':nonce,'checked_at':iso()})
        result=store.document(key)[0]
        return ('VERIFIED' if result and result.get('nonce')==nonce else 'ISSUE',{'scope':'Dedicated diagnostic document write/read round-trip'},'')
    run('storage','round_trip',storage_probe)
    run('storage','source_archives_iam',lambda:('NOT_VERIFIED',{'scope':'Source CSV integrity, bucket permissions, billing and backup restore'},'Needs cloud-side verification; JSON traversal does not inspect these resources'))
    return checks


def anomaly(findings,code):findings[code]=findings.get(code,0)+1

def inspect_candles(data,findings):
    from data_calendar import session
    candles=data.get('candles',{});timestamps=set();minutes=set();day=data.get('day');item=data.get('instrument',{})
    daily=not bool(data.get('variant',{}).get('interval'))
    for key,c in candles.items():
        try:
            values=[c[k] for k in ('open','high','low','close','volume','timestamp')]
            if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in values):raise ValueError('numeric')
            o,h,l,close,volume,ts=values
            if min(o,h,l,close,volume)<0 or h<max(o,l,close) or l>min(o,close):anomaly(findings,'invalid_ohlc')
            if ts%60:anomaly(findings,'off_minute_timestamp')
            at=datetime.fromtimestamp(ts,timezone.utc).astimezone(IST)
            if at.date().isoformat()!=day:anomaly(findings,'wrong_partition_date')
            identity=(ts,c.get('strike'))
            if identity in timestamps:anomaly(findings,'duplicate_candle_identity')
            timestamps.add(identity);minutes.add(at.strftime('%H:%M'))
        except (KeyError,ValueError,TypeError,OverflowError,OSError):anomaly(findings,'malformed_candle')
    if not candles:anomaly(findings,'empty_candle_partition')
    try:
        calendar=session(datetime.fromisoformat(day).date(), 'NSE' if item.get('exchange')=='NSE' else item.get('exchange','UNKNOWN'))
        if daily:anomaly(findings,'daily_grid_not_verified')
        elif calendar['state']=='REGULAR' and str(data.get('variant',{}).get('interval'))=='1':
            expected={f'{(555+n)//60:02d}:{(555+n)%60:02d}' for n in range(375)}
            missing=expected-minutes
            if missing:anomaly(findings,'partitions_with_missing_minutes');findings['missing_minutes']=findings.get('missing_minutes',0)+len(missing)
            if minutes-expected:anomaly(findings,'off_session_minutes')
        else:anomaly(findings,'calendar_or_interval_not_verified')
    except (ValueError,TypeError):anomaly(findings,'calendar_or_interval_not_verified')


def inspect_document(namespace,key,data,findings):
    if data is None:anomaly(findings,'missing_on_read');return
    if not isinstance(data,dict):anomaly(findings,'unexpected_document_schema');return
    if namespace=='journal':anomaly(findings,'journal_semantics_not_verified');return
    if namespace=='records':
        kind=data.get('kind','unknown');anomaly(findings,'record_kind:'+str(kind)[:60])
        if kind=='dhan_history':
            from dhan_data import verify_record
            try:
                result=verify_record(data.get('data',{}))
                if result.get('status')=='INVALID':anomaly(findings,'invalid_saved_history')
                if result.get('status')=='EMPTY':anomaly(findings,'empty_saved_history')
            except Exception:anomaly(findings,'invalid_saved_history')
        else:anomaly(findings,'record_semantics_not_verified')
        return
    prefix=key.split('/')[0];anomaly(findings,'namespace:'+prefix)
    if prefix=='candles':inspect_candles(data,findings)
    elif prefix=='results':
        result=data.get('result') or {};state=result.get('status','missing')
        anomaly(findings,'outcome:'+str(state))
        if state in ('failed','partial','empty','permission_blocked','unsupported'):anomaly(findings,'unresolved_outcomes')
        if result.get('duplicate_timestamps'):anomaly(findings,'reported_duplicate_timestamps')
        if result.get('quarantined_rows'):anomaly(findings,'quarantined_candles')
    elif prefix=='jobs':
        state=data.get('state');anomaly(findings,'job_state:'+str(state))
        if state in ('token_required','permission_blocked','request_review'):anomaly(findings,'jobs_requiring_action')
        if data.get('system_review_required'):anomaly(findings,'jobs_internal_error')
        if state=='running' and time.time()-stamp(data.get('updated_at',data.get('created_at')))>900:anomaly(findings,'stalled_job_candidate')
    elif prefix=='responses':
        if not data.get('endpoint') or 'raw' not in data:anomaly(findings,'raw_response_schema')
        else:
            from dhan_data import candle_count,option_count
            endpoint=data['endpoint'];raw=data['raw']
            try:
                if endpoint in ('charts/historical','charts/intraday'):candle_count(raw)
                elif endpoint=='charts/rollingoption':option_count(raw,'CALL');option_count(raw,'PUT')
                elif endpoint in ('optionchain','optionchain/expirylist','marketfeed/quote','marketfeed/ltp','marketfeed/ohlc'):
                    if not isinstance(raw,dict) or raw.get('status')=='failure':anomaly(findings,'rejected_raw_snapshot')
                    else:anomaly(findings,'snapshot_freshness_not_verified')
                else:anomaly(findings,'raw_response_semantics_not_verified')
            except Exception:anomaly(findings,'invalid_raw_history')
    elif prefix=='streams':
        from data_recorder import packets
        # Bound decompression before JSON parsing, even for malformed archives.
        import io
        encoded=data.get('payload','')
        if not isinstance(encoded,str) or len(encoded)>12*1024*1024:raise ValueError('archive size')
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded,validate=True))) as archive:raw=archive.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('archive expansion limit')
        frames=json.loads(raw);family=data.get('family')
        if family not in ('live','depth20','depth200'):raise ValueError('archive family')
        for frame in frames:
            binary=base64.b64decode(frame['binary_base64'],validate=True)
            try:
                for packet in packets(binary,family):anomaly(findings,'binary_packets_framed')
            except ValueError:anomaly(findings,'invalid_archived_frame')
        anomaly(findings,'archive_exchange_identity_not_verified')
    elif prefix=='catalogue':
        for item in data.get('items',[]):
            if item.get('reason'):anomaly(findings,'unmapped_instruments')
    elif prefix=='quarantine':anomaly(findings,'quarantine_archives')
    elif prefix not in ('control','system','doctor','research','series','datasets'):
        anomaly(findings,'unclassified_namespace:'+prefix[:60])


def repair(collector,scan):
    from data_system import enabled,restore_subscriptions
    store=collector.store;actions=[]
    def attempt(name,operation):
        try:operation();actions.append({'action':name,'state':'REQUESTED','at':iso(),'verified':False})
        except Exception as error:actions.append({'action':name,'state':'BLOCKED','diagnostic':fingerprint('doctor_repair',error),'verified':False})
    if not enabled(store):return [{'action':'automatic_collection_repairs','state':'BLOCKED','reason':'Collection is paused; Doctor will not enable it or resume manually paused jobs.','verified':False}]
    config=store.document('control/data-system')[0] or {}
    attempt('restore_retained_subscriptions',lambda:restore_subscriptions(store,config.get('credential_revision','initial')))
    recovery=store.document('control/gap-recovery')[0] or {}
    if recovery.get('enabled'):
        attempt('request_gap_rescan',lambda:replace(store,'control/gap-recovery',lambda old:{**(old or {}),'next_scan_at':0}))
    actions.append({'action':'feed_reconnect_and_transient_retry','state':'MANAGED_BY_EXISTING_WORKER',
                    'reason':'Existing bounded reconnect/retry loops retain ownership; Doctor opens no competing sockets.','verified':False})
    return actions


def inventory_reads(store,namespace,names,deadline):
    """Read at most eight objects concurrently, commit results in cursor order.

    Only reads run in threads: findings, counters and the cursor still have one
    writer. Stop between batches, never checkpoint past an uninspected object.
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        for offset in range(0,len(names),8):
            if offset and time.monotonic()>deadline:return
            batch=names[offset:offset+8]
            pending={key:pool.submit(store.diagnostic_read,namespace,key) for key in batch
                     if not (namespace=='documents' and key.startswith('doctor/'))}
            for key in batch:
                if key not in pending:
                    yield key,None,None
                    continue
                try:yield key,pending[key].result(),None
                except Exception as error:yield key,None,error


def tick(collector,page_size=100):
    """One bounded step. A failing object/check never silently stops other areas."""
    store=collector.store;scan=store.document(KEY)[0] or {}
    if not pending(store):return False
    if scan.get('retry_after',0)>time.time():return True
    if not 1<=page_size<=100:raise ValueError('Diagnostic page size must be 1–100')
    owner=uuid.uuid4().hex;claimed=False
    def claim(old):
        nonlocal claimed
        if not old or old.get('id')!=scan['id'] or old.get('state')=='STOPPED' or old.get('lease_until',0)>time.time():return old
        if any(old.get(k)!=scan.get(k) for k in ('phase','namespace','cursor')):return old
        claimed=True;return {**old,'lease_owner':owner,'lease_until':time.time()+600}
    replace(store,KEY,claim)
    if not claimed:return True
    try:
        phase=scan['phase']
        if phase=='checks':
            checks=system_checks(collector,scan)
            update(store,scan['id'],lambda s:{**s,'state':'SCANNING','checks':checks,'phase':'inventory','last_error':None,'retry_after':0})
        elif phase=='inventory':
            namespace=NAMESPACES[scan['namespace']];names=store.diagnostic_names(namespace,scan.get('cursor',''),page_size)
            findings=dict(scan.get('findings',{}));examples=list(scan.get('examples',[]));errors=0;details=[]
            visited=[];deadline=time.monotonic()+12
            for key,data,read_error in inventory_reads(store,namespace,names,deadline):
                visited.append(key)
                # Do not recursively inspect Doctor's own mutable report/probes.
                if namespace=='documents' and key.startswith('doctor/'):continue
                before=dict(findings)
                try:
                    if read_error is not None:raise read_error
                    inspect_document(namespace,key,data,findings)
                except Exception as error:
                    errors+=1;anomaly(findings,'unreadable_records')
                    detail={'namespace':namespace,'key':key,'diagnostic':fingerprint('doctor_record',error)}
                    details.append(detail)
                    if len(examples)<30:examples.append(detail)
                delta={k:v-before.get(k,0) for k,v in findings.items() if v>before.get(k,0) and not k.startswith(('namespace:','record_kind:','outcome:','job_state:')) and k!='binary_packets_framed'}
                if delta:
                    detail={'namespace':namespace,'key':key,'findings':delta}
                    details.append(detail)
                    if len(examples)<30:examples.append(detail)
            if details:
                page_key='doctor/findings/'+scan['id']+'/'+hashlib.sha256((namespace+':'+scan.get('cursor','')).encode()).hexdigest()
                def save_details(old):
                    entries={r['namespace']+':'+r['key']:r for r in (old or {}).get('items',[])}
                    for r in details:
                        identity=r['namespace']+':'+r['key'];entries[identity]={**entries.get(identity,{}),**r}
                    return {'namespace':namespace,'items':list(entries.values()),'checked_at':iso()}
                replace(store,page_key,save_details)
            inspected_count=sum(not (namespace=='documents' and key.startswith('doctor/')) for key in visited)
            inspected={**scan.get('inspected',{}),namespace:scan.get('inspected',{}).get(namespace,0)+inspected_count}
            finished=len(names)<page_size and len(visited)==len(names);index=scan['namespace']+int(finished)
            update(store,scan['id'],lambda s:{**s,'inspected':inspected,'findings':findings,'examples':examples,
                'scan_errors':s.get('scan_errors',0)+errors,'namespace':index,'cursor':'' if finished else visited[-1],
                'last_error':None,'retry_after':0,
                'phase':('repair' if s['repair_requested'] else 'verify') if index==len(NAMESPACES) else 'inventory',
                'state':'REPAIRING' if index==len(NAMESPACES) and s['repair_requested'] else 'SCANNING'})
        elif phase=='repair':
            actions=repair(collector,scan)
            update(store,scan['id'],lambda s:{**s,'repairs':actions,'phase':'verify','state':'VERIFYING','last_error':None,'retry_after':0})
        elif phase=='verify':
            after=system_checks(collector,scan)
            findings=scan.get('findings',{})
            serious=sum(v for k,v in findings.items() if k in ('unreadable_records','invalid_ohlc','malformed_candle','invalid_archived_frame','unresolved_outcomes','partitions_with_missing_minutes','jobs_requiring_action','invalid_raw_history','invalid_saved_history','wrong_partition_date','duplicate_candle_identity'))
            after.append(check('storage','inventory_traversal','ISSUE' if scan.get('scan_errors') else 'VERIFIED',{'inspected':scan.get('inspected',{}),'unreadable':findings.get('unreadable_records',0),'scan_errors':scan.get('scan_errors',0),'scope':'Paginated traversal completed; concurrent writes and semantic checks remain separate'}))
            after.append(check('history','archive_findings','ISSUE' if serious else 'NOT_VERIFIED',{'findings':findings},'Review saved examples; a clean traversal does not establish absent instrument/date coverage'))
            update(store,scan['id'],lambda s:{**s,'before_checks':s.get('checks',[]),'checks':after,
                'phase':'finished','state':'FINISHED_WITH_FINDINGS','finished_at':iso(),'coverage_verified':False,'last_error':None,'retry_after':0,
                'repair_verification':'Post-repair checks recorded; requested actions alone are not verified repairs.'})
    except Exception as error:
        # Listing/network failure retains the current cursor for automatic retry.
        diagnostic=fingerprint('doctor_scan',error)
        update(store,scan['id'],lambda s:{**s,'scan_errors':s.get('scan_errors',0)+1,
            'last_error':diagnostic,'retry_after':time.time()+60})
    finally:
        def release(old):
            if old and old.get('lease_owner')==owner:return {**old,'lease_until':0}
            return old
        replace(store,KEY,release)
    return pending(store)
