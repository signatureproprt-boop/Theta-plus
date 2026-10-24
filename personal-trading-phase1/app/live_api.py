"""Authenticated live diagnostics, watchlist control and browser socket fanout."""
import asyncio
import hmac
import json
from datetime import datetime
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from app.config import settings, read_secret
from app.db import session
from app.models import Instrument, MasterIdentity, Watchlist, LiveGap, SystemState
from app.policy import validate_identity
from app.rate_gate import redis_client
from app.live_runtime import CHANNEL


class Selection(BaseModel):
    exchange_segment: str = Field(max_length=24)
    security_id: str = Field(pattern=r'^[1-9][0-9]{0,9}$')


def install(app, protected):
    router = APIRouter(dependencies=protected)
    @router.get('/live/status')
    def status():
        redis = redis_client()
        with session() as db:
            state = db.get(SystemState, 'live-health')
            owner = db.get(SystemState, 'collection-owner')
            return {'live': state.data if state else {'state': 'NOT_STARTED'},
                'owner': owner.data if owner else {'state': 'AWAITING_VERIFIED_HANDOVER'},
                'gap_jobs': dict(db.execute(select(LiveGap.status, func.count()).group_by(LiveGap.status)).all()),
                'redis_unpersisted_entries': redis.xlen('theta:live:pending'),
                'mcx': 'HOLD', 'all_market_tick_coverage': False}
    @router.get('/live/watchlist')
    def watchlist():
        with session() as db:
            return {'items': [{'exchange_segment': r.exchange_segment, 'security_id': r.security_id}
                             for r in db.scalars(select(Watchlist).order_by(Watchlist.id)).all()]}
    @router.post('/live/watchlist', status_code=201)
    def add_watchlist(item: Selection):
        with session() as db:
            identity = (item.exchange_segment, item.security_id)
            instrument, meta = db.get(Instrument, identity), db.get(MasterIdentity, identity)
            if not instrument or not meta:
                raise HTTPException(409, 'VERIFY_MASTER_IDENTITY_FIRST')
            try:
                validate_identity(instrument.exchange_segment, instrument.instrument_type)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if not instrument.mapping_verified or meta.blocked:
                raise HTTPException(409, 'NEEDS_MAPPING')
            if db.scalar(select(func.count()).select_from(Watchlist)) >= settings().live_connections*5000:
                raise HTTPException(409, 'SINGLE_CONNECTION_LIMIT')
            db.execute(insert(Watchlist).values(**item.model_dump()).on_conflict_do_nothing())
            db.commit()
        return {'state': 'SAVED', 'refresh_within_seconds': 30}
    @router.get('/live/gaps')
    def gaps():
        with session() as db:
            rows = db.scalars(select(LiveGap).order_by(LiveGap.next_attempt_at.desc()).limit(100)).all()
            return {'items': [{'id': str(r.id), 'exchange_segment': r.exchange_segment,
                'security_id': r.security_id, 'start': r.start, 'end': r.end,
                'status': r.status, 'attempts': r.attempts, 'evidence': r.evidence} for r in rows],
                'coverage_verified': False, 'missing_ticks_recoverable': False}
    @router.get('/live/latest')
    def latest():
        return {'items': [json.loads(v) for v in redis_client().hvals('theta:live:latest')],
                'freshness_verified': False}
    app.include_router(router)

    @app.websocket('/live/ws')
    async def feed(ws: WebSocket):
        origin = ws.headers.get('origin')
        if origin and urlparse(origin).netloc != ws.headers.get('host'):
            await ws.close(code=1008); return
        await ws.accept()
        pubsub, redis = None, None
        try:
            # Browser APIs cannot set X-API-Key on the handshake. Authenticate
            # in the first TLS message, never a query URL or access log.
            data = await asyncio.wait_for(ws.receive_json(), 10)
            key = read_secret(settings().api_key_file)
            supplied = data.get('api_key', '') if isinstance(data, dict) else ''
            if len(key) < 32 or not isinstance(supplied, str) or not hmac.compare_digest(key.encode(), supplied.encode()):
                await ws.close(code=1008); return
            redis = redis_client(); pubsub = redis.pubsub(ignore_subscribe_messages=True)
            await asyncio.to_thread(pubsub.subscribe, CHANNEL)
            await ws.send_json({'state': 'SUBSCRIBED', 'replay': False})
            # At-most-once UI fanout. PostgreSQL/Redis stream persistence is separate.
            while True:
                msg = await asyncio.to_thread(pubsub.get_message, timeout=1)
                if msg:
                    await asyncio.wait_for(ws.send_text(msg['data'].decode()), 2)
                else:
                    await asyncio.wait_for(ws.send_json({'kind': 'heartbeat'}), 2)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception:
            await ws.close(code=1011)
        finally:
            if pubsub: pubsub.close()
            if redis: redis.close()
