"""Single-user research application. Run via python run.py; local-only by default."""
from __future__ import annotations
import asyncio
import csv
import io
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError, Field
from models import Dataset, ContextRecord, PlanInput, JournalInput, Experiment, Snapshot, Candle, Model, CONTEXT_KEYS
from store import Store
from provider import Dhan, ProviderError
from market_board import MarketBoard
from live_feed import relay
from engine import bars5, chart_evidence, timeframe_trend, option_evidence, decision, size_plan, replay, VERSION, session_open, IST
from credentials import DhanCredentials,CredentialError
from admin_auth import admin_config, require_admin
from token_check import check_token
from research_audit import audit_historical
from dhan_data import fetch_history,fetch_chain,verify_record

ROOT=Path(__file__).parent

class DhanTokenInput(Model):
    token: str = Field(min_length=20,max_length=4096)

class AuditInput(Model):
    day: date

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

class ContractMetadata(Model):
    security_id: str = Field(min_length=1,max_length=60)
    expiry: date
    strike: float = Field(gt=0,allow_inf_nan=False)
    side: str
    lot_size: int = Field(gt=0,le=100000)

def create_app(db_path=None):
    public_workspace=os.environ.get('THETA_AUTH_MODE')=='public'
    bucket=os.environ.get('THETA_GCS_BUCKET')
    if os.environ.get('K_SERVICE') and (not bucket or (not public_workspace and not os.environ.get('THETA_API_KEY'))):
        raise RuntimeError('Cloud Run requires durable THETA_GCS_BUCKET and THETA_API_KEY')
    if bucket and db_path is None:
        from cloud_store import CloudStore
        store=CloudStore(bucket)
    else:
        store=Store(db_path or os.environ.get('THETA_DB_PATH',str(ROOT/'data/theta.sqlite3')))
    credential_source=DhanCredentials()
    provider=Dhan(credentials=credential_source);refresh_lock=asyncio.Lock();last_error=None;last_attempt=0.0
    board=MarketBoard(provider,store)
    active_streams=0
    def metadata():
        rows=store.rows('contract_metadata',1)
        manual={r['security_id']:r for r in rows[0]['data']['contracts']} if rows else {}
        return {**manual,**board.options}
    async def refresh():
        nonlocal last_error,last_attempt
        if refresh_lock.locked():raise ProviderError('A refresh is already running')
        async with refresh_lock:
            if time.monotonic()-last_attempt<30:return
            last_attempt=time.monotonic()
            try:
                try:await board.catalog()
                except Exception:pass
                snap,candles=await provider.refresh(metadata())
                store.add('live_snapshot',snap.model_dump(mode='json'))
                store.add('live_candles',{'candles':[c.model_dump(mode='json') for c in candles]})
                last_error=None
            except Exception as exc:
                last_error=str(exc) if isinstance(exc,ProviderError) else 'Provider returned invalid data; update rejected'
                raise ProviderError(last_error) from None
    async def poll():
        while True:
            if os.environ.get('THETA_REQUEST_REFRESH')!='true' and provider.configured and session_open(datetime.now(timezone.utc)):
                try:await refresh()
                except ProviderError:pass
            await asyncio.sleep(30)
    @asynccontextmanager
    async def lifespan(app):
        task=asyncio.create_task(poll())
        yield
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass
    app=FastAPI(title='Theta+ Research',version='2.0.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store

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

    def get_dataset(id):
        row=store.get('dataset',id)
        if not row:raise HTTPException(404,'Dataset not found')
        return Dataset.model_validate(row['data'])

    def observations(dataset_id):
        if dataset_id:
            dataset=get_dataset(dataset_id)
            return sorted(dataset.snapshots,key=lambda x:x.timestamp),dataset.candles,'HISTORICAL_IMPORT'
        rows=store.rows('live_snapshot',150)
        candles=store.rows('live_candles',1)
        return sorted([Snapshot.model_validate(r['data']) for r in rows],key=lambda s:s.timestamp),[Candle.model_validate(c) for c in candles[0]['data']['candles']] if candles else [],'PROVIDER'

    @app.get('/health-check')
    def health():
        # Public release identity only; no data, credentials or provider state.
        return {'status':'ok','version':VERSION,'execution':'NO_BROKER_EXECUTION',
                'revision':os.environ.get('K_REVISION','local')}

    @app.get('/api/status')
    def status():
        return {'version':VERSION,'dhan_configured':provider.configured,'provider_error':last_error,
                'client_id_configured':bool(credential_source.client_id),
                'auth_mode':'public' if public_workspace else 'workspace_key',
                'token_screen_enabled':bool(credential_source.secret_name) and admin_config(),
                'credential_check':(store.rows('credential_status',1) or [{}])[0].get('data'),
                'request_refresh':os.environ.get('THETA_REQUEST_REFRESH')=='true' and session_open(datetime.now(timezone.utc)),
                'execution':'NO_BROKER_EXECUTION','strategy_validation':'UNVALIDATED_RESEARCH',
                'context_sources':'Timestamped user import; automatic global/FII feeds not connected',
                'metadata_loaded':len(metadata()),'server_time':datetime.now(timezone.utc).isoformat()}

    @app.post('/api/refresh')
    async def refresh_route():
        try:await refresh()
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None
        return {'ok':True}

    @app.get('/api/admin/config')
    def google_admin_config():
        return {'enabled':admin_config(), 'method':'admin_password'}

    @app.post('/api/admin/check')
    def check_admin_password(request:Request):
        require_admin(request)
        return {'authorised':True}

    @app.post('/api/dhan/token')
    async def rotate_dhan_token(payload:DhanTokenInput,request:Request):
        require_admin(request)
        if not credential_source.secret_name:raise HTTPException(503,'Token storage needs one-time Secret Manager setup')
        result=await check_token(payload.token,credential_source.client_id)
        try:credential_source.update_token(payload.token)
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        store.add('credential_status',result)
        return {'updated':True,'message':'Token checked and saved. Market data must be verified separately.',**result}

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

    @app.get('/api/dhan/collected')
    def dhan_collected():
        return {kind:[{'id':r['id'],**r['data']['metadata']} for r in store.rows(kind,500)]
                for kind in ('dhan_history','dhan_chain')}

    @app.get('/api/research/setup')
    def research_setup_status():
        rows=store.rows('research_setup',1)
        return rows[0]['data'] if rows else {'state':'NOT_RUN','backtest_ready':False,
            'validation':'UNVALIDATED','message':'Cloud collection worker has not published a report.'}

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

    @app.get('/api/markets/live')
    async def markets_live(request:Request):
        nonlocal active_streams
        if not provider.configured:raise HTTPException(503,'Dhan credentials not configured')
        if active_streams>=3:raise HTTPException(429,'Close other live tabs and retry')
        active_streams+=1
        try:
            await board.catalog()
            instruments=list(board.instruments.values())
            if not instruments:raise ValueError('No instruments')
        except Exception:
            active_streams-=1
            raise HTTPException(503,'Instrument catalog unavailable; live subscription not started') from None
        async def stream():
            nonlocal active_streams
            try:
                async for event in relay(request,instruments,credential_source):yield event
            finally:active_streams-=1
        return StreamingResponse(stream(),media_type='application/x-ndjson',headers={'X-Accel-Buffering':'no'})

    @app.get('/api/markets')
    def markets():return board.view()

    @app.post('/api/markets/refresh')
    async def markets_refresh():
        try:return await board.refresh()
        except ProviderError as exc:raise HTTPException(503,str(exc)) from None

    @app.get('/api/schema')
    def schema():
        return {'dataset':Dataset.model_json_schema(),'context':ContextRecord.model_json_schema(),
                'contract_metadata':ContractMetadata.model_json_schema(),'context_keys':CONTEXT_KEYS,
                'notes':['All timestamps include timezone offsets. Candles are one-minute START timestamps.',
                         'quote_at must be an observed quote timestamp, not an invented import time.',
                         'Dataset import must include source/provenance; imported data is never labelled live.',
                         'Use actual exchange lot sizes for each expiry. No numeric defaults are supplied.']}

    @app.post('/api/datasets')
    def dataset_add(payload:Dataset):
        now=datetime.now(timezone.utc)
        if any(s.timestamp>now for s in payload.snapshots) or any(c.timestamp>now for c in payload.candles):
            raise HTTPException(422,'Historical observations cannot be in the future')
        id=store.add('dataset',payload.model_dump(mode='json'))
        return {'id':id,'snapshots':len(payload.snapshots),'candles':len(payload.candles)}

    @app.get('/api/datasets')
    def datasets():
        return [{'id':r['id'],'name':r['data']['name'],'provenance':r['data']['provenance'],
                 'snapshots':len(r['data']['snapshots']),'candles':len(r['data']['candles'])} for r in store.rows('dataset',30)]

    @app.post('/api/context')
    def context_add(records:list[ContextRecord]):
        if not 1<=len(records)<=100:raise HTTPException(422,'Provide 1–100 context records')
        now=datetime.now(timezone.utc)
        if any(r.published_at>now or r.observed_at>now for r in records):raise HTTPException(422,'Future publications cannot be imported as known context')
        store.add('context',{'records':[r.model_dump(mode='json') for r in records]})
        return {'saved':len(records)}

    def context_at(at):
        found={}
        for batch in store.rows('context',200):
            for raw in batch['data']['records']:
                r=ContextRecord.model_validate(raw)
                if r.published_at<=at and (r.key not in found or r.published_at>found[r.key].published_at):found[r.key]=r
        return [({'key':key,'status':'MISSING'} if key not in found else {
            **found[key].model_dump(mode='json'),'status':'AVAILABLE' if found[key].valid_until>=at else 'STALE',
            'origin':'USER_IMPORTED','used_in_signal':False}) for key in CONTEXT_KEYS]

    @app.post('/api/contracts')
    def contracts_add(records:list[ContractMetadata]):
        if not 1<=len(records)<=5000:raise HTTPException(422,'Provide 1–5000 contract records')
        if any(r.side not in ('CE','PE') for r in records):raise HTTPException(422,'side must be CE or PE')
        if len({r.security_id for r in records})!=len(records):raise HTTPException(422,'Duplicate security IDs')
        store.add('contract_metadata',{'contracts':[r.model_dump(mode='json') for r in records]})
        return {'saved':len(records)}

    @app.get('/api/overview')
    def overview(dataset_id:int|None=None,strategy:str='structure'):
        if strategy not in ('structure','ema','opening_range'):raise HTTPException(422,'Unsupported strategy')
        snaps,candles,mode=observations(dataset_id)
        if not snaps:return {'status':'NO_DATA','mode':mode,'context':context_at(datetime.now(timezone.utc)),
                             'message':'Configure Dhan on the server or import timestamped historical data.'}
        snap=snaps[-1];now=datetime.now(timezone.utc);at=snap.timestamp
        complete_bars=bars5(candles)
        chart=chart_evidence(complete_bars,at,strategy);options=option_evidence(snap,snaps[:-1])
        trend={'intraday_15m':timeframe_trend(complete_bars,at,15),
               'prior_daily':timeframe_trend(complete_bars,at,0),
               'used_in_default_decision':False}
        d=decision(chart,options);trend_decision=decision(chart,options,'trend_combined',trend)
        fresh=0<=(now-at).total_seconds()<=60 and last_error is None
        state='HISTORICAL' if mode=='HISTORICAL_IMPORT' else ('RECENT_PROVIDER_SNAPSHOT' if fresh else 'STALE')
        allowed=mode=='HISTORICAL_IMPORT' or fresh and session_open(now)
        bars=[b for b in complete_bars if b['end']<=at][-80:]
        return {'status':state,'mode':mode,'timestamp':at.isoformat(),'source':snap.source,'spot':snap.spot,
                'strategy_version':VERSION,'chart':chart,'trend':trend,'options':options,'decision':d if allowed else 'WAIT',
                'trend_decision':trend_decision if allowed else 'WAIT',
                'decision_label':'Research candidate only; strategy unvalidated',
                'contracts':[c.model_dump(mode='json') for c in snap.contracts],
                'bars':[{**b,'start':b['start'].isoformat(),'end':b['end'].isoformat()} for b in bars],
                'context':context_at(at if mode=='HISTORICAL_IMPORT' else now)}

    @app.post('/api/plans')
    def plan(payload:PlanInput):
        snaps,_,mode=observations(payload.dataset_id)
        if not snaps:raise HTTPException(409,'No market data')
        snap=snaps[-1];now=datetime.now(timezone.utc)
        if mode=='PROVIDER' and (not 0<=(now-snap.timestamp).total_seconds()<=60 or last_error is not None or not session_open(now)):
            raise HTTPException(409,'Provider data is stale, unavailable, from the future or outside weekday session hours')
        c=next((c for c in snap.contracts if c.security_id==payload.security_id),None)
        if not c:raise HTTPException(404,'Contract not found')
        result=size_plan(c,payload.capital,payload.risk_budget,payload.stop_quote,payload.costs_per_lot,
                         payload.slippage_per_unit,snap.timestamp,payload.max_spread_pct)
        id=str(uuid.uuid4());created=now.isoformat()
        result.update({'plan_id':id,'created_at':created,'contract':c.model_dump(mode='json'),
                       'as_of':snap.timestamp.isoformat(),'source':snap.source,'mode':mode,
                       'input':payload.model_dump(mode='json'),'kind':'RESEARCH_PLAN_NOT_A_FILL'})
        store.add('plan',result)
        return result

    @app.post('/api/journal')
    def journal_add(payload:JournalInput):
        found=next((r['data'] for r in store.rows('plan',500) if r['data']['plan_id']==payload.plan_id),None)
        if not found:raise HTTPException(404,'Saved plan not found')
        if not found['allowed']:raise HTTPException(409,'Blocked plan cannot be recorded as eligible')
        if found['mode']=='PROVIDER' and (datetime.now(timezone.utc)-datetime.fromisoformat(found['created_at'])).total_seconds()>60:
            raise HTTPException(409,'Plan expired; calculate again with a fresh snapshot')
        added=store.journal_add(payload.plan_id,{'plan':found,'note':payload.note,'state':'RESEARCH_PLAN_NOT_EXECUTED'})
        return {'saved':added,'duplicate':not added}

    @app.get('/api/journal')
    def journal():return store.journal_rows()

    @app.get('/api/journal.csv')
    def journal_csv():
        stream=io.StringIO();writer=csv.writer(stream)
        writer.writerow(['Recorded UTC','State','Mode','Contract','Expiry','Side','Quantity','Planned loss','Note'])
        def safe(v):
            t=str(v)
            return "'"+t if t.lstrip().startswith(('=','+','-','@')) else t
        for r in store.journal_rows():
            p=r['plan'];c=p['contract']
            writer.writerow([safe(x) for x in [r['created_at'],r['state'],p['mode'],c['security_id'],c['expiry'],c['side'],p['quantity'],p['planned_loss'],r['note']]])
        return Response(stream.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="theta-journal.csv"'})

    @app.post('/api/experiments')
    def run_experiment(payload:Experiment):
        dataset=get_dataset(payload.dataset_id)
        result=replay(dataset,payload)
        result['dataset_id']=payload.dataset_id
        id=store.add('experiment',result)
        return {'id':id,**result}

    @app.get('/api/experiments')
    def experiments():
        return [{'id':r['id'],'created_at':r['created_at'],'status':r['data']['status'],
                 'summary':r['data']['summary'],'experiment':r['data']['experiment']} for r in store.rows('experiment',50)]

    @app.get('/api/experiments/{id}')
    def experiment(id:int):
        r=store.get('experiment',id)
        if not r:raise HTTPException(404,'Experiment not found')
        return {'id':id,**r['data']}

    @app.get('/')
    def index():return FileResponse(ROOT/'static/index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return app

app=create_app()
