"""Serve the retained Theta Plus application from the new FastAPI deployment."""
import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from fastapi import HTTPException, Request
from app.config import read_secret, settings
from app.legacy_store import PostgresStore
from app.models import utcnow


def mount_workspace(app):
    root = Path('/srv/legacy')
    if not root.exists():
        root = Path(__file__).resolve().parents[2] / 'live-app'
    if not root.exists():
        raise RuntimeError('LEGACY_WORKSPACE_SOURCE_MISSING')
    sys.path.insert(0, str(root))
    os.environ['THETA_EMBEDDED'] = '1'
    # The API remains loopback-only; the retained UI keeps its existing read mode.
    os.environ['THETA_AUTH_MODE'] = 'public'
    os.environ.pop('THETA_COLLECTION_WORKER', None)
    os.environ.pop('THETA_GCS_BUCKET', None)
    os.environ.setdefault('THETA_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver,theta-plus-jw4jpkebxa-el.a.run.app')
    spec = importlib.util.spec_from_file_location('theta_retained_server', root / 'server.py')
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    from credentials import CredentialError
    from provider import ProviderError, DATA_PATHS, quota_family

    class FileCredentials:
        pair_secret = 'local-protected-files'
        secret_name = ''
        @property
        def client_id(self):
            return read_secret(settings().dhan_client_id_file)
        def token(self):
            return read_secret(settings().dhan_access_token_file)
        @property
        def configured(self):
            try:
                return bool(self.client_id and self.token())
            except OSError:
                return False
        def save_pair(self, client_id, token):
            if not client_id or not 20 <= len(token) <= 4096 or any(c.isspace() for c in token):
                raise CredentialError('Invalid Dhan connection input')
            try:
                # Write in place so existing worker bind mounts see token rotation.
                Path(settings().dhan_client_id_file).write_text(client_id+'\n')
                Path(settings().dhan_access_token_file).write_text(token+'\n')
            except OSError:
                raise CredentialError('Protected credential files are not writable by the API') from None
            return True
        def update_token(self, token):
            return self.save_pair(self.client_id, token)

    store = PostgresStore()
    workspace = legacy.create_app(store_override=store, credentials_override=FileCredentials())
    provider = workspace.state.collector.provider

    async def shared_post(path, body, *, _commodity_probe=False):
        import json
        from app.rate_gate import RateGate, GateDeferred
        if path not in DATA_PATHS:
            raise ProviderError('Unsupported data endpoint', code='ENDPOINT')
        serialized = json.dumps(body)
        if _commodity_probe or any(s in serialized for s in ('MCX', 'NSE_COMM', 'FUTCOM', 'OPTFUT')):
            raise ProviderError('Commodity integration remains HOLD', code='COMMODITY_HOLD')
        if not provider.configured:
            raise ProviderError('Dhan credentials required', code='DH-901', status=401)
        gate = RateGate(provider.credentials.client_id)
        try:
            family, spacing = quota_family(path)
            slot = await asyncio.to_thread(gate.acquire, family, int(spacing*1000))
        except GateDeferred:
            raise ProviderError('Shared account budget is waiting', code='ACCOUNT_GATE', status=429) from None
        started = utcnow()
        clock = time.monotonic()
        code, status, outcome = None, None, 'ROWS_RECEIVED'
        try:
            return await asyncio.to_thread(provider._request, path, body)
        except ProviderError as exc:
            code, status = exc.code, exc.status
            outcome = 'DH907_REVIEW' if code == 'DH-907' else 'PROVIDER_REVIEW'
            if status == 429 or code in ('805', 'DH-904'):
                outcome = 'RATE_LIMIT'
                await asyncio.to_thread(gate.cooldown, 60)
            raise
        finally:
            # Separate log family; never persist raw exception messages or secrets.
            await asyncio.to_thread(store.add, 'unified_provider_attempt', {
                'endpoint': path, 'http_started_at': started.isoformat(),
                'duration_ms': int((time.monotonic()-clock)*1000), 'provider_code': code,
                'http_status': status, 'outcome': outcome, 'gate_wait_ms': slot.wait_ms,
                'since_previous_gate_ms': slot.previous_ms,
                'rate_limit_evidence': status == 429 or code in ('805', 'DH-904')})
    provider.post = shared_post

    @workspace.middleware('http')
    async def one_collection_owner(request: Request, call_next):
        from fastapi.responses import JSONResponse
        try:
            encoded = read_secret('/run/secrets/legacy_admin_hash')
            if encoded: os.environ['THETA_TOKEN_ADMIN_HASH'] = encoded
        except OSError:
            pass
        path = request.url.path
        # Preserve old history/research/paper APIs. The retired background engine
        # cannot be restarted in the new deployment; Celery and live own ingestion.
        if request.method != 'GET' and (path.startswith('/api/data-system/') or
                path.startswith('/api/collection/') or path.startswith('/api/system-doctor/')):
            return JSONResponse({'detail': 'Collection is managed by the unified backend; use /market/status and /live/status'}, 409)
        response = await call_next(request)
        return response

    @app.get('/api/v2/system')
    def unified_status():
        from app.db import session
        from app.models import SystemState
        with session() as db:
            migration = db.get(SystemState, 'legacy-migration')
            owner = db.get(SystemState, 'collection-owner')
            connections = db.get(SystemState, 'live-connections')
            live = db.get(SystemState, 'live-health')
            return {'system': 'Theta Plus', 'storage': 'PostgreSQL + TimescaleDB',
                    'mcx': 'HOLD', 'legacy_background_engine': 'DISABLED_IN_THIS_DEPLOYMENT',
                    'migration': migration.data if migration else {'state': 'AWAITING_ARCHIVE_IMPORT'},
                    'live': live.data if live else {'state': 'NOT_STARTED'},
                    'live_connections': connections.data if connections else {'connections': []},
                    'owner': owner.data if owner else {'state': 'WAITING_FOR_VERIFIED_HANDOVER'}}

    app.mount('/', workspace)
