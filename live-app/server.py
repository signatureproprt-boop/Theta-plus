"""Theta Plus data foundation. Legacy trading analysis has been removed."""
from __future__ import annotations
import os,secrets
from pathlib import Path
from datetime import datetime,timezone,date
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse
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
ROOT=Path(__file__).parent
class DhanTokenInput(Model):
    token: str = Field(min_length=20,max_length=4096)

class DhanConnectionInput(Model):
    client_id: str | None = Field(default=None,min_length=1,max_length=60)
    token: str | None = Field(default=None,min_length=20,max_length=4096)

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
    provider=Dhan(credentials=credential_source)
    board=MarketBoard(provider,store)
    app=FastAPI(title='Theta Plus Data Foundation',version=VERSION,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store

    @app.get('/api/status')
    def status():
        return {'version':VERSION,'dhan_configured':provider.configured,
            'auth_mode':'public' if public_workspace else 'workspace_key',
            'token_screen_enabled':bool(credential_source.pair_secret or credential_source.secret_name) and admin_config(),
            'credential_check':(store.rows('credential_status',1) or [{}])[0].get('data'),
            'execution':'NO_BROKER_EXECUTION','analysis_state':'NOT_IMPLEMENTED',
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
        if not payload.client_id or not payload.token:raise HTTPException(422,'Enter Client ID and a new token')
        if not credential_source.pair_secret:raise HTTPException(503,'Admin connection storage needs one-time setup')
        result=await check_token(payload.token,payload.client_id)
        try:credential_source.save_pair(payload.client_id,payload.token)
        except CredentialError as exc:raise HTTPException(503,str(exc)) from None
        result['checked_at']=datetime.now(timezone.utc).isoformat()
        store.add('credential_status',result)
        return {'saved':True,**result}

    @app.post('/api/dhan/token')
    async def rotate_dhan_token(payload:DhanTokenInput,request:Request):
        require_admin(request)
        if not (credential_source.pair_secret or credential_source.secret_name):raise HTTPException(503,'Token storage needs one-time Secret Manager setup')
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
