"""Theta Plus data foundation and versioned read-only research. Legacy engine remains retired."""
from __future__ import annotations
import os,secrets
import asyncio
from pathlib import Path
from datetime import datetime,timezone,date
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError,Field
from models import Model
from store import Store
from provider import Dhan,ProviderError
from market_board import MarketBoard
from runtime import VERSION
from credentials import DhanCredentials,CredentialError
from admin_auth import admin_config,require_admin
from token_check import check_token
from research_audit import audit_historical
from dhan_data import fetch_history,fetch_chain,verify_record
from collection import Collector,CollectionPlan
from instrument_directory import InstrumentDirectory
from history_coverage import coverage_report
from worker import launch_worker,worker_status
from research import workspace, risk_plan, RiskInput, PaperNote, ReplayInput, replay_report, RULE_VERSION
from live_research import stream as research_live_stream
from session_chart import refresh_session,refresh_context
from typing import Literal
from request_review import diagnose,repair_verified_range
from paper_ledger import PaperOpen,PaperClose,open_trade,close_trade,view as paper_view
ROOT=Path(__file__).parent
class DhanTokenInput(Model):
    token: str = Field(min_length=20,max_length=4096)

class DhanConnectionInput(Model):
    client_id: str | None = Field(default=None,min_length=1,max_length=60)
    token: str | None = Field(default=None,min_length=20,max_length=4096)

class AuditInput(Model):
    day: date

class LiveInput(Model):
    symbol: Literal['nifty','banknifty']
    contract_id: str = Field(default='',max_length=40,pattern=r'^[0-9]*$')

class DoctorInput(Model):
    repair: bool = False

class DoctorCursor(Model):
    after: str = Field(default='',max_length=240)

class DhanHistoryInput(Model):
    kind: str
    index: str
    start: date
    end: date
    strike: str = 'ATM'
    side: str = 'CALL'
    expiry_flag: str = 'MONTH'
    expiry_code: int = Field(default=1,ge=1,le=5)

class DhanChainInput(Model):
    index: str

class InstrumentSelection(Model):
    key: str = Field(min_length=1,max_length=128)
    catalogue_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

class InstrumentHistoryInput(InstrumentSelection):
    start: date
    end: date
    family: Literal['daily','intraday'] = 'intraday'
    interval: Literal[1,5,15,25,60] = 1

def create_app(db_path=None):
    public_workspace=os.environ.get('THETA_AUTH_MODE')=='public'
    bucket=os.environ.get('THETA_GCS_BUCKET')
    if os.environ.get('K_SERVICE') and (not bucket or (not public_workspace and not os.environ.get('THETA_API_KEY'))):
        raise RuntimeError('Cloud Run requires durable storage and configured access mode')
    if bucket and db_path is None:
        from cloud_store import CloudStore
        store=CloudStore(bucket)
    else:store=Store(db_path or os.environ.get('THETA_DB_PATH',str(ROOT/'data/theta.sqlite3')))
    credential_source=DhanCredentials()
    provider=Dhan(credentials=credential_source,store=store)
    board=MarketBoard(provider,store)
    app=FastAPI(title='Theta Plus Data Foundation',version=VERSION,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store
    collector=Collector(provider,store)
    app.state.collector=collector
    directory=InstrumentDirectory(collector)
    app.state.instrument_directory=directory

    @app.get('/api/instruments')
    def instruments(q:str='',segment:str='',page:int=0,active_only:bool=True,category:str='all',include_quotes:bool=False):
        return directory.search(q,segment,page,active_only,category,include_quotes)

    @app.get('/api/instruments/overview')
    def instrument_overview(keys:str=''):
        return directory.overview(keys)

    @app.get('/api/instruments/saved-history')
    def instrument_saved_history(key:str,catalogue_sha256:str,start:date,end:date,family:str='intraday',interval:int=1):
        return directory.saved_history(key,catalogue_sha256,start,end,family,interval)

    @app.get('/api/instruments/recorded')
    def instrument_recorded(key:str,catalogue_sha256:str):
        return directory.recorded(key,catalogue_sha256)

    @app.post('/api/instruments/quote')
    async def instrument_quote(payload:InstrumentSelection,request:Request):
        require_admin(request)
        try:return await directory.quote(payload.key,payload.catalogue_sha256)
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None

    @app.post('/api/instruments/history')
    async def instrument_history(payload:InstrumentHistoryInput,request:Request):
        require_admin(request)
        try:return await directory.history(payload.key,payload.catalogue_sha256,payload.start,payload.end,payload.family,payload.interval)
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None


    @app.post('/api/research/live')
    async def research_live(payload:LiveInput,request:Request):
        require_admin(request)
        if not provider.configured:raise HTTPException(503,'Dhan credentials not configured')
        return StreamingResponse(research_live_stream(provider,store,board,payload.symbol,request,payload.contract_id),
            media_type='text/event-stream',headers={'X-Accel-Buffering':'no','Cache-Control':'no-store'})

    @app.post('/api/research/session')
    async def research_session(payload:LiveInput,request:Request):
        require_admin(request)
        if not provider.configured:raise HTTPException(503,'Dhan credentials not configured')
        try:
            result=await refresh_session(provider,store,board,payload.symbol)
            result['context']=await refresh_context(provider,store,board,payload.symbol)
            from breadth import refresh as refresh_breadth
            result['context']['breadth']=await refresh_breadth(provider,store,board,payload.symbol)
            result['workspace']=await asyncio.to_thread(workspace,store,payload.symbol,options=board.options,contract_id=payload.contract_id)
            return result
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None

    @app.get('/api/research/workspace')
    async def research_workspace(symbol:str='nifty',contract_id:str=''):
        if symbol not in ('nifty','banknifty'):raise HTTPException(422,'Choose NIFTY or BANKNIFTY')
        if provider.configured:await board.catalog()
        result=await asyncio.to_thread(workspace,store,symbol,options=board.options,contract_id=contract_id)
        result['collection']=collector.jobs()
        result['worker']=worker_status(store)
        result['credential_check']=(store.rows('credential_status',1) or [{}])[0].get('data')
        result['markets']=board.view()
        return result

    @app.get('/api/data-validator')
    def live_data_validator(symbol:Literal['nifty','banknifty','india_vix']|None=None):
        from data_validator import report
        return report(store,symbol=symbol)

    @app.post('/api/research/risk')
    def research_risk(payload:RiskInput):
        return risk_plan(payload)

    @app.post('/api/research/paper/list')
    def research_paper_view(request:Request):
        require_admin(request)
        return paper_view(store)

    @app.post('/api/research/paper')
    async def research_paper_open(payload:PaperOpen,request:Request):
        require_admin(request)
        await board.catalog()
        return await asyncio.to_thread(open_trade,store,payload,board.options)

    @app.post('/api/research/paper/{tid}/close')
    def research_paper_close(tid:str,payload:PaperClose,request:Request):
        require_admin(request)
        return close_trade(store,tid,payload)

    @app.post('/api/research/journal')
    def research_journal_write(payload:PaperNote,request:Request):
        require_admin(request)
        key='research-notes/'+payload.id
        existing=store.document(key)[0]
        if existing is not None:
            if existing['input']!=payload.model_dump():raise HTTPException(409,'Note ID already used with different contents')
            return existing
        note={'input':payload.model_dump(),'rule_version':RULE_VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
              'kind':'OBSERVATION_NOT_TRADE','execution':'NO_BROKER_EXECUTION'}
        # Fixed document key is the canonical idempotent journal; no duplicate side writes.
        if not store.put_document(key,note,0):raise HTTPException(409,'Journal changed concurrently; retry')
        return note

    @app.get('/api/research/journal')
    def research_notes(after:str=''):
        import re
        if after and not re.fullmatch(r'research-notes/[a-f0-9]{32}',after):raise HTTPException(422,'Invalid journal cursor')
        names=store.document_names('research-notes/',after,101)
        return {'entries':[store.document(k)[0] for k in names[:100]],'next_cursor':names[99] if len(names)>100 else None,
                'scope':'Immutable observation journal. No fills or trade P&L are inferred.'}

    @app.post('/api/research/replay')
    async def research_replay(payload:ReplayInput):
        try:return await asyncio.to_thread(replay_report,store,payload)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post('/api/collection/profile/check')
    async def collection_profile(request:Request):
        require_admin(request)
        try:result=await check_token(credential_source.token(),credential_source.client_id)
        except CredentialError:raise HTTPException(503,'Dhan credentials unavailable') from None
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        store.add('credential_status',result)
        from segment_probe import check
        try:probe=await check(collector)
        except Exception:probe={'state':'PROBE_UNAVAILABLE','mapping_changed':False}
        return {**result,'unmapped_commodity_check':probe}

    @app.get('/api/collection/catalogue/segment-probe')
    def collection_segment_probe_report():
        from segment_probe import public_report
        return public_report(store)

    @app.get('/api/collection/catalogue')
    def collection_catalogue():
        cat=collector.catalogue()
        return {k:v for k,v in (cat or {}).items() if k!='pages'}

    @app.get('/api/collection/catalogue/page')
    def collection_catalogue_page(page:int=0):
        return collector.catalogue_page(page)

    @app.post('/api/collection/catalogue/refresh')
    async def collection_catalogue_refresh(request:Request):
        require_admin(request)
        try:
            cat=await collector.refresh_catalogue()
            return {k:v for k,v in cat.items() if k!='pages'}
        except (ValueError,UnicodeError):raise HTTPException(502,'Catalogue format invalid; previous catalogue retained') from None
        except Exception as exc:
            if isinstance(exc,HTTPException):raise
            raise HTTPException(502,'Catalogue refresh failed; previous catalogue retained') from None

    @app.get('/api/collection/jobs')
    def collection_jobs(after:str=''):
        if after and (not after.startswith('jobs/') or len(after)!=37):raise HTTPException(422,'Invalid cursor')
        return collector.jobs(after)

    @app.post('/api/collection/jobs')
    async def collection_create(payload:CollectionPlan,request:Request):
        require_admin(request)
        return await asyncio.to_thread(collector.create,payload)

    @app.get('/api/collection/jobs/{jid}')
    def collection_job(jid:str):
        return collector.summary(collector.get_job(jid))

    @app.post('/api/collection/jobs/{jid}/run')
    async def collection_run(jid:str,request:Request):
        require_admin(request)
        return await collector.run(jid)

    @app.post('/api/collection/jobs/{jid}/diagnose')
    async def collection_diagnose(jid:str,request:Request,payload:dict|None=None):
        require_admin(request)
        key=(payload or {}).get('result_key')
        if key is not None and not isinstance(key,str):raise HTTPException(422,'Choose a result key')
        return await diagnose(collector,jid,key)

    @app.post('/api/collection/jobs/{jid}/repair')
    async def collection_repair(jid:str,request:Request,payload:dict):
        require_admin(request)
        key=payload.get('result_key')
        if not isinstance(key,str):raise HTTPException(422,'Choose a diagnosed historical result')
        return await asyncio.to_thread(repair_verified_range,collector,jid,key)

    @app.post('/api/collection/jobs/{jid}/{action}')
    def collection_control(jid:str,action:str,request:Request):
        require_admin(request)
        from data_system import enabled
        if action=='resume' and enabled(store) and set(collector.get_job(jid)['config']['families'])<=set(('live','depth20','depth200')):
            raise HTTPException(409,'Pause continuous recording before starting socket samples; the recorder owns the feed connections')
        result=collector.control(jid,action)
        if action=='resume' and result['state']=='running' and os.environ.get('THETA_COLLECTION_WORKER'):
            result['runner']=launch_worker(store)
        return result

    @app.get('/api/collection/engine')
    def collection_engine():
        return {'sdk':provider.capabilities(),'worker':worker_status(store)}

    @app.get('/api/data-system')
    def data_system_status():
        from data_system import status
        return status(store)

    @app.post('/api/system-doctor/status')
    def system_doctor_status(request:Request):
        require_admin(request)
        from system_doctor import report
        return report(store)

    @app.post('/api/system-doctor/findings')
    def system_doctor_findings(payload:DoctorCursor,request:Request):
        require_admin(request)
        from system_doctor import finding_pages
        return finding_pages(store,payload.after)

    @app.post('/api/system-doctor/start')
    async def system_doctor_start(payload:DoctorInput,request:Request):
        require_admin(request)
        if not os.environ.get('THETA_COLLECTION_WORKER'):
            raise HTTPException(503,'Configure the durable worker before starting a full scan')
        from system_doctor import start,pending,app_inventory,application_probes,report
        if await asyncio.to_thread(pending,store):raise HTTPException(409,'A scan is already pending; refresh its progress')
        probes=await application_probes(app)
        await asyncio.to_thread(start,store,payload.repair,app_inventory(app),probes)
        try:runner=await asyncio.to_thread(launch_worker,store)
        except HTTPException:
            runner={'state':'DISPATCH_PENDING','note':'Scan retained; existing private watchdog can dispatch it. Check worker permissions if it stays queued.'}
        return {**await asyncio.to_thread(report,store),'runner':runner}

    @app.post('/api/system-doctor/stop')
    def system_doctor_stop(request:Request):
        require_admin(request)
        from system_doctor import KEY,report
        from collection import replace
        replace(store,KEY,lambda old:{**(old or {}),'state':'STOPPED','stopped_at':datetime.now(timezone.utc).isoformat()})
        return report(store)

    @app.post('/api/data-system/start')
    def data_system_start(request:Request):
        require_admin(request)
        if not os.environ.get('THETA_COLLECTION_WORKER'):raise HTTPException(503,'Configure the durable worker and watchdog before enabling the data system')
        from data_system import start
        state=start(collector)
        return {'enabled':state['enabled'],'runner':launch_worker(store)}

    @app.post('/api/data-system/pause')
    def data_system_pause(request:Request):
        require_admin(request)
        from collection import replace
        replace(store,'control/data-system',lambda old:{**(old or {}),'enabled':False})
        return {'enabled':False,'message':'Continuous recording and automatic schedules paused; historical jobs retain their separate controls.'}

    @app.get('/api/data-system/gaps')
    def data_system_gaps():return store.rows('ingestion_gap',100)

    @app.post('/api/data-system/watchdog')
    def data_system_watchdog(payload:dict,request:Request):
        require_admin(request)
        if payload.get('worker')!=os.environ.get('THETA_COLLECTION_WORKER') or payload.get('schedule')!='*/5 * * * *':
            raise HTTPException(422,'Watchdog does not match the configured private worker')
        from collection import replace
        return replace(store,'control/data-watchdog',lambda _:{'reported_configured':True,'verified':False,
                       'schedule':payload['schedule'],'worker':payload['worker'],
                       'note':'Deployment reported scheduler configured. Restart dispatch still requires a live acceptance test.'})

    @app.get('/api/data-system/subscriptions')
    def data_system_subscriptions():return store.document('system/subscription-plan')[0] or {}

    @app.get('/api/data-system/latest/{family}')
    def data_system_latest(family:str):
        if family not in ('live','live-extra','depth20','depth200'):raise HTTPException(422,'Unknown feed family')
        return store.document('system/latest/'+family)[0] or {}

    @app.get('/api/collection/session-recovery')
    def collection_session_recovery():
        return store.document('system/session-recovery')[0] or {'state':'NOT_CHECKED','coverage_verified':False}

    @app.get('/api/collection/recovery')
    def collection_recovery_status():
        return store.document('control/gap-recovery')[0] or {'enabled':False}

    @app.post('/api/collection/recovery/start')
    def collection_recovery_start(request:Request):
        require_admin(request)
        from collection import replace
        result=replace(store,'control/gap-recovery',lambda old:{**(old or {}),'enabled':True,'next_scan_at':0})
        if os.environ.get('THETA_COLLECTION_WORKER'):launch_worker(store)
        return result

    @app.get('/api/collection/jobs/{jid}/results')
    def collection_results(jid:str,after:str=''):
        return collector.results(jid,after)

    @app.post('/api/collection/retry')
    async def collection_retry(payload:dict,request:Request):
        require_admin(request)
        jid=payload.get('job_id');key=payload.get('key')
        if not isinstance(jid,str) or not isinstance(key,str):raise HTTPException(422,'Choose a result to retry')
        return await asyncio.to_thread(collector.retry,jid,key)

    @app.get('/api/collection/export/{key:path}')
    def collection_export(key:str):
        import re
        if not re.fullmatch(r'((responses|quarantine)/[a-f0-9]{64}|candles/[a-f0-9]{64}/[0-9]{4}-[0-9]{2}-[0-9]{2}|streams/[0-9]{4}-[0-9]{2}-[0-9]{2}/(live|live-extra|depth20|depth200)/[a-f0-9]{32}/[0-9]{8})',key):
            raise HTTPException(422,'Invalid export key')
        result=store.document(key)[0]
        if result is None:raise HTTPException(404,'Data not found')
        return result

    @app.get('/api/status')
    def status():
        return {'version':VERSION,'dhan_configured':provider.configured,'data_sdk':provider.capabilities(),
            'auth_mode':'public' if public_workspace else 'workspace_key',
            'token_screen_enabled':bool(credential_source.pair_secret or credential_source.secret_name) and admin_config(),
            'credential_check':(store.rows('credential_status',1) or [{}])[0].get('data'),
            'execution':'NO_BROKER_EXECUTION','analysis_state':'READ_ONLY_RESEARCH_UNVALIDATED',
            'legacy_analysis':'REMOVED','saved_data':'RETAINED',
            'server_time':datetime.now(timezone.utc).isoformat()}

    @app.get('/api/archive/{kind}')
    def archived_records(kind:str):
        if kind=='journal':return store.journal_rows()
        if kind not in {'dataset','context','contract_metadata','experiment','plan','live_snapshot','live_candles','data_audit','research_setup'}:
            raise HTTPException(404,'Unknown archive type')
        return store.rows(kind,500)

    @app.middleware('http')
    async def boundary(request:Request,call_next):
        host=request.headers.get('host','').split(':')[0].lower()
        allowed=set(os.environ.get('THETA_ALLOWED_HOSTS','localhost,127.0.0.1,testserver').split(','))
        if host not in allowed:return JSONResponse({'detail':'Host not allowed'},status_code=400)
        if request.url.path.startswith('/api/'):
            key=os.environ.get('THETA_API_KEY','')
            given=request.headers.get('authorization','').removeprefix('Bearer ')
            if not public_workspace and key and not secrets.compare_digest(key,given):return JSONResponse({'detail':'Workspace access key required'},status_code=401)
            origin=request.headers.get('origin')
            if origin and origin!=f'{request.url.scheme}://{request.headers.get("host")}':return JSONResponse({'detail':'Cross-origin API access rejected'},status_code=403)
            if request.method=='POST':
                if not request.headers.get('content-type','').startswith('application/json'):
                    return JSONResponse({'detail':'Use application/json'},status_code=415)
                # Stream-bound request size; never buffer an unbounded upload.
                data=bytearray()
                async for part in request.stream():
                    data.extend(part)
                    if len(data)>12*1024*1024:return JSONResponse({'detail':'Upload limit is 12 MiB'},status_code=413)
                request._body=bytes(data)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(ValidationError)
    async def validation_error(request,exc):
        return JSONResponse({'detail':'Input validation failed. Check dates, prices and required fields.'},status_code=422)

    @app.get('/health-check')
    def health():
        # Public release identity only; no data, credentials or provider state.
        return {'status':'ok','version':VERSION,'execution':'NO_BROKER_EXECUTION',
                'revision':os.environ.get('K_REVISION','local')}

    @app.get('/api/admin/config')
    def google_admin_config():
        return {'enabled':admin_config(), 'method':'admin_password'}

    @app.post('/api/admin/check')
    def check_admin_password(request:Request):
        require_admin(request)
        return {'authorised':True}

    @app.post('/api/admin/dhan/settings')
    def dhan_settings(request:Request):
        require_admin(request)
        try:client=credential_source.client_id
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        return {'client_id':client,'token_saved':provider.configured,
                'editable':bool(credential_source.pair_secret)}

    @app.post('/api/admin/dhan/test')
    async def dhan_test(payload:DhanConnectionInput,request:Request):
        require_admin(request)
        try:
            result=await check_token(payload.token or credential_source.token(),payload.client_id or credential_source.client_id)
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        return {**result,'saved':False,'message':'Authentication checked. Live quote freshness is checked separately.'}

    @app.post('/api/admin/dhan/save')
    async def dhan_save(payload:DhanConnectionInput,request:Request):
        require_admin(request)
        try:client_id=payload.client_id or credential_source.client_id
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        if not client_id or not payload.token:raise HTTPException(422,'Enter a new token; Client ID is needed only for the first setup')
        if not credential_source.pair_secret:raise HTTPException(503,'Admin connection storage needs one-time setup')
        result=await check_token(payload.token,client_id)
        try:credential_source.save_pair(client_id,payload.token)
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        store.add('credential_status',result)
        from data_system import credentials_saved
        recovery=await asyncio.to_thread(credentials_saved,collector)
        if recovery['enabled'] and os.environ.get('THETA_COLLECTION_WORKER'):
            try:recovery['runner']=await asyncio.to_thread(launch_worker,store)
            except HTTPException:recovery['worker_warning']='Credentials saved; worker dispatch pending. Check automatic data system / watchdog.'
        return {'saved':True,'data_system':recovery,**result}

    @app.post('/api/dhan/token')
    async def rotate_dhan_token(payload:DhanTokenInput,request:Request):
        require_admin(request)
        if not (credential_source.pair_secret or credential_source.secret_name):raise HTTPException(503,'Token storage needs one-time Secret Manager setup')
        result=await check_token(payload.token,credential_source.client_id)
        try:credential_source.update_token(payload.token)
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        store.add('credential_status',result)
        from data_system import credentials_saved
        recovery=await asyncio.to_thread(credentials_saved,collector)
        if recovery['enabled'] and os.environ.get('THETA_COLLECTION_WORKER'):
            try:recovery['runner']=await asyncio.to_thread(launch_worker,store)
            except HTTPException:recovery['worker_warning']='Credentials saved; worker dispatch pending. Check automatic data system / watchdog.'
        return {'updated':True,'message':'Token checked and saved. Market data must be verified separately.','data_system':recovery,**result}

    @app.post('/api/research/audit')
    async def audit_route(payload:AuditInput):
        if not provider.configured:raise HTTPException(503,'Dhan credentials not configured')
        try:report=await audit_historical(provider,payload.day)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        store.add('data_audit',report)
        return report

    @app.post('/api/dhan/history')
    async def dhan_history(payload:DhanHistoryInput):
        if not provider.configured:raise HTTPException(503,'Dhan credentials not configured')
        try:
            await board.catalog()
            instrument_key=payload.index+'_future' if payload.kind=='future' else payload.index
            return await fetch_history(provider,store,**payload.model_dump(),instrument=board.instruments.get(instrument_key))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None

    @app.post('/api/dhan/chain')
    async def dhan_chain(payload:DhanChainInput):
        if not provider.configured:raise HTTPException(503,'Dhan credentials not configured')
        try:
            await board.catalog()
            return await fetch_chain(provider,store,payload.index,board.instruments.get(payload.index))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None

    @app.get('/api/dhan/coverage')
    def dhan_coverage(start:date,end:date,include_rows:bool=False):
        try:return coverage_report(store,start,end,include_rows)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.get('/api/dhan/collected')
    def dhan_collected(kind:str|None=None):
        if kind is not None and kind not in ('dhan_history','dhan_chain'):raise HTTPException(422,'Unknown record kind')
        return {kind:[{'id':r['id'],**r['data']['metadata']} for r in store.rows(kind,500)]
                for kind in ((kind,) if kind else ('dhan_history','dhan_chain'))}

    @app.get('/api/dhan/collected/{kind}/{record_id}')
    def dhan_collected_raw(kind:str,record_id:int):
        if kind not in ('dhan_history','dhan_chain'):raise HTTPException(404,'Unknown data type')
        row=store.get(kind,record_id)
        if not row:raise HTTPException(404,'Record not found')
        return row['data']

    @app.get('/api/dhan/collected/{kind}/{record_id}/verify')
    def dhan_collected_verify(kind:str,record_id:int):
        if kind not in ('dhan_history','dhan_chain'):raise HTTPException(404,'Unknown data type')
        row=store.get(kind,record_id)
        if not row:raise HTTPException(404,'Record not found')
        try:return verify_record(row['data'])
        except (ValueError,TypeError,OverflowError,KeyError):
            raise HTTPException(422,'Saved Dhan response cannot be verified') from None

    @app.get('/api/markets')
    def markets():return board.view()

    @app.post('/api/markets/refresh')
    async def markets_refresh():
        try:return await board.refresh()
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None

    @app.get('/')
    def index():return FileResponse(ROOT/'static/index.html')

    @app.api_route('/api/{retired:path}',methods=['GET','POST'])
    def retired_analysis(retired:str):
        # Registered after retained routes below. No old analysis is calculated.
        raise HTTPException(410,'Old analysis setup removed. Saved data are retained.')

    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return app

app=create_app()
