"""One live owner, Redis fanout, restart-safe tick batches and durable gap plans."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from redis.exceptions import ResponseError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.db import engine, session
from app.models import LiveTick, SystemState, LiveGap, utcnow
from app.market_collection import put_state
from app.rate_gate import redis_client
from app.live_feed import DhanLiveClient, SessionStarted, load_watchlist, credentials, credential_revision

LOG = logging.getLogger(__name__)
STREAM, CHANNEL, GROUP = 'theta:live:pending', 'theta:live:events', 'postgres'
CONSUMER = 'singleton-writer'


def collection_owned():
    with session() as db:
        owner = db.get(SystemState, 'collection-owner')
        return bool(settings().live_enabled and owner and owner.data.get('state') == 'NEW_BACKEND')


def record_session(info, shard=0):
    # Small durable receipt only; lengthy per-instrument planning is off the socket loop.
    value = {'id': uuid.uuid4().hex, 'generation': info.generation,
        'connected_at': info.connected_at.isoformat(),
        'disconnected_at': info.disconnected_at.isoformat() if info.disconnected_at else None,
        'instruments': [list(i) for i in info.instruments]}
    with session() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(73462005)'))
        old = db.get(SystemState, 'live-session-pending:'+str(shard))
        if old and old.data.get('id'):
            previous = old.data.get('disconnected_at') or old.data['connected_at']
            value['disconnected_at'] = min(previous, value['disconnected_at'] or value['connected_at'])
        put_state(db, 'live-session-pending:'+str(shard), value)
        put_state(db, 'live-health', {'state': 'SUBSCRIPTIONS_SENT',
            'checked_at': utcnow().isoformat(), 'generation': info.generation,
            'watchlist_count': len(info.instruments), 'freshness_verified': False})
        db.commit()


def process_pending_session(shard=0):
    from app.gaps import plan_session
    with session() as db:
        row = db.get(SystemState, 'live-session-pending:'+str(shard))
        data = dict(row.data) if row else {}
    if not data.get('id'):
        return
    info = SessionStarted(data['generation'], datetime.fromisoformat(data['connected_at']),
        datetime.fromisoformat(data['disconnected_at']) if data.get('disconnected_at') else None,
        tuple(tuple(i) for i in data['instruments']))
    plan_session(info)
    with session() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(73462005)'))
        row = db.get(SystemState, 'live-session-pending:'+str(shard))
        if row and row.data.get('id') == data['id']:
            row.data = {'state': 'PLANNED', 'checked_at': utcnow().isoformat()}
        db.commit()


def persist_batch(entries):
    """DB first, Redis ACK after commit. Duplicate delivery doesn't duplicate ticks."""
    rows = []
    for event_id, fields in entries:
        data = json.loads(fields[b'event'])
        rows.append(dict(event_id=data['event_id'], timestamp=datetime.fromisoformat(data['received_at']),
            exchange_segment=data.get('exchange_segment', 'STATUS'), security_id=data.get('security_id', '0'),
            trade_time=datetime.fromisoformat(data['trade_time']) if data.get('trade_time') else None,
            payload=data))
    with session() as db:
        if db.scalar(text('SELECT pg_database_size(current_database())')) >= settings().history_database_soft_gib*1024**3:
            raise RuntimeError('STORAGE_SOFT_LIMIT')
        if rows:
            db.execute(insert(LiveTick).values(rows).on_conflict_do_nothing())
            put_state(db, 'live-health', {'state': 'RECEIVING', 'checked_at': utcnow().isoformat(),
                'last_received_at': rows[-1]['timestamp'].isoformat(), 'batch_rows': len(rows),
                'freshness_verified': False})
        db.commit()


async def batch_writer(redis):
    try:
        await asyncio.to_thread(redis.xgroup_create, STREAM, GROUP, id='0', mkstream=True)
    except ResponseError as exc:
        if 'BUSYGROUP' not in str(exc):
            raise
    pending = True
    batch = []
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            messages = await asyncio.to_thread(redis.xreadgroup, GROUP, CONSUMER,
                {STREAM: '0' if pending else '>'}, count=max(1, 100-len(batch)), block=1000)
            entries = messages[0][1] if messages else []
            if pending:
                if entries:
                    await asyncio.to_thread(persist_batch, entries)
                    ids = [entry[0] for entry in entries]
                    pipe = redis.pipeline(transaction=True)
                    pipe.xack(STREAM, GROUP, *ids); pipe.xdel(STREAM, *ids)
                    await asyncio.to_thread(pipe.execute)
                    continue
                pending = False
            batch.extend(entries)
            if batch and (len(batch) >= 100 or asyncio.get_running_loop().time() >= deadline):
                await asyncio.to_thread(persist_batch, batch)
                ids = [entry[0] for entry in batch]
                pipe = redis.pipeline(transaction=True)
                pipe.xack(STREAM, GROUP, *ids); pipe.xdel(STREAM, *ids)
                await asyncio.to_thread(pipe.execute)
                LOG.info('LIVE_BATCH_COMMITTED rows=%d', len(batch))
                batch.clear()
                deadline = asyncio.get_running_loop().time()+5
        except asyncio.CancelledError:
            # Uncommitted messages remain in the stream PEL for next owner startup.
            raise
        except Exception as exc:
            LOG.warning('LIVE_BATCH_RETRY error_type=%s pending_rows=%d', type(exc).__name__, len(batch))
            # A failed ACK can replay committed entries; PK deduplicates.
            batch.clear(); pending = True
            await asyncio.sleep(5)


async def maintenance():
    from app.gaps import dispatch_gaps
    last_revision = None
    settled = 0
    while True:
        try:
            for shard in range(settings().live_connections):
                await asyncio.to_thread(process_pending_session, shard)
            revision = credential_revision(await asyncio.to_thread(credentials))
            if revision != last_revision:
                with session() as db:
                    for gap in db.scalars(select(LiveGap).where(LiveGap.status == 'AUTH_REQUIRED',
                            LiveGap.attempts < settings().max_fetch_attempts)).all():
                        gap.status, gap.next_attempt_at, gap.last_dispatched_at = 'QUEUED', utcnow(), None
                    db.commit()
                last_revision = revision
            await asyncio.to_thread(dispatch_gaps)
            # Finalized-minute catch-up covers the settling tail and restarts.
            if asyncio.get_running_loop().time() >= settled:
                watchlist = await asyncio.to_thread(load_watchlist, settings().live_connections*5000)
                if watchlist:
                    for shard in range(settings().live_connections):
                        scope = watchlist[shard*5000:(shard+1)*5000]
                        if scope:
                            await asyncio.to_thread(record_session, SessionStarted(0, utcnow(), None, scope), shard)
                settled = asyncio.get_running_loop().time()+300
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('LIVE_MAINTENANCE_RETRY error_type=%s', type(exc).__name__)
        await asyncio.sleep(5)


async def retained_depth(family):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from app.legacy_store import PostgresStore
    from app.models import Instrument, MasterIdentity
    from app.policy import validate_identity
    root = Path('/srv/legacy')
    if not root.exists(): root = Path(__file__).resolve().parents[2]/'live-app'
    sys.path.insert(0,str(root))
    from data_recorder import record
    class Credentials:
        @property
        def client_id(self): return credentials()[0]
        def token(self): return credentials()[1]
    store = PostgresStore()
    def scope(_):
        plan=store.document('system/subscription-plan')[0] or {}
        selected=[]
        with session() as db:
            for old in plan.get('scopes',{}).get(family,[]):
                identity=(old.get('segment'),str(old.get('security_id')))
                item,meta=db.get(Instrument,identity),db.get(MasterIdentity,identity)
                if not item or not meta or meta.blocked or not item.mapping_verified:continue
                try:validate_identity(item.exchange_segment,item.instrument_type)
                except ValueError:continue
                if meta.expiry and meta.expiry < utcnow().astimezone(__import__('app.dhan',fromlist=['IST']).IST).date():continue
                if item.instrument_type != old.get('instrument'):continue
                if old.get('expiry') and (not meta.expiry or meta.expiry.isoformat()!=old['expiry']):continue
                selected.append(old)
        return {'revision':plan.get('feed_revisions',{}).get(family,plan.get('revision')), 'instruments':selected}
    while True:
        try:
            await record(SimpleNamespace(credentials=Credentials()),store,family,scope,collection_owned)
        except asyncio.CancelledError:raise
        except Exception as exc:
            LOG.warning('RETAINED_DEPTH_RETRY family=%s error_type=%s',family,type(exc).__name__)
            await asyncio.sleep(30)


async def run_owned(conn):
    redis = redis_client()
    process_id = uuid.uuid4().hex
    async def event(value):
        data = {**value, 'event_id': process_id + ':' + uuid.uuid4().hex}
        raw = json.dumps(data, allow_nan=False)
        pipe = redis.pipeline(transaction=True)
        pipe.xadd(STREAM, {'event': raw})  # Never MAXLEN-trim unpersisted data.
        if value.get('kind') == 'previous_close':
            pipe.hset('theta:live:previous', value['exchange_segment']+':'+value['security_id'], str(value['previous_close']))
        if value.get('kind') == 'tick':
            # Auxiliary/OI packets must never overwrite the last price tick.
            pipe.hset('theta:live:latest', value['exchange_segment']+':'+value['security_id'], raw)
        pipe.publish(CHANNEL, raw)
        await asyncio.to_thread(pipe.execute)
    def client(shard):
        def watchlist():
            return load_watchlist(settings().live_connections*5000)[shard*5000:(shard+1)*5000]
        async def connected(info):
            await asyncio.to_thread(record_session, info, shard)
        async def shard_event(value):
            await event({**value, 'connection': shard})
        return DhanLiveClient(shard_event, connected, watchlist_loader=watchlist)
    tasks = [asyncio.create_task(client(shard).run()) for shard in range(settings().live_connections)]
    tasks += [asyncio.create_task(batch_writer(redis)), asyncio.create_task(maintenance()),
              asyncio.create_task(retained_depth('depth20')), asyncio.create_task(retained_depth('depth200'))]
    try:
        while True:
            await asyncio.sleep(5)
            # Lock and ownership lost => close socket before another local process starts.
            await asyncio.to_thread(conn.execute, text('SELECT 1'))
            await asyncio.to_thread(conn.commit)
            if not await asyncio.to_thread(collection_owned):
                return
            for task in tasks:
                if task.done():
                    task.result()
                    raise RuntimeError('LIVE_CHILD_STOPPED')
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        redis.close()


async def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            if not await asyncio.to_thread(collection_owned):
                LOG.info('LIVE_HOLD_AWAITING_VERIFIED_MIGRATION_AND_LEGACY_STOP')
                await asyncio.sleep(30)
                continue
            with engine().connect() as conn:
                locked = conn.scalar(text('SELECT pg_try_advisory_lock(73462004)'))
                conn.commit()
                if not locked:
                    await asyncio.sleep(10)
                    continue
                try:
                    await run_owned(conn)
                finally:
                    conn.rollback()
                    conn.execute(text('SELECT pg_advisory_unlock(73462004)'))
                    conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('LIVE_RUNTIME_RETRY error_type=%s', type(exc).__name__)
            await asyncio.sleep(10)


if __name__ == '__main__':
    import signal
    async def serve():
        task = asyncio.create_task(main())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(serve())
