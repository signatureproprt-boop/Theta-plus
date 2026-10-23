"""Whole-master discovery and exact-identity Dhan data access.

The disposable SQLite index contains public instrument metadata only. Raw and
normalized provider observations continue to use the existing durable store.
"""
import asyncio
import anyio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading

from fastapi import HTTPException
from collection import SEGMENTS, contract_expiry
from market_board import quote_row
from runtime import IST

SEGMENT_NAMES = sorted(set(SEGMENTS.values()))


class InstrumentDirectory:
    def __init__(self, collector):
        self.collector = collector
        self.temporary = tempfile.TemporaryDirectory(prefix='theta-instruments-')
        self.lock = threading.Lock()

    def index(self):
        cat = self.collector.catalogue()
        if not cat:
            raise HTTPException(409, 'Refresh full instrument catalogue in Collect all data first')
        sha = cat.get('sha256', '')
        if not re.fullmatch('[a-f0-9]{64}', sha):
            raise HTTPException(503, 'Catalogue identity is invalid')
        target = Path(self.temporary.name) / (sha + '.sqlite3')
        if target.is_file():
            return target, cat
        if not self.lock.acquire(timeout=1):
            raise HTTPException(503, 'Instrument search index is preparing; retry shortly')
        pending = target.with_suffix('.building')
        try:
            if target.is_file():
                return target, cat
            pending.unlink(missing_ok=True)
            with closing(sqlite3.connect(pending)) as db:
                db.execute('CREATE TABLE instruments (position INTEGER PRIMARY KEY, key TEXT UNIQUE, segment TEXT, expiry TEXT, search TEXT, document TEXT)')
                pages = [p for p in cat['pages'] if p['group'] != 'rolling-underlyings']
                position = 0
                # Bound outstanding page reads; do not load a second full master
                # into RAM while the recorder and historical collector are active.
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for offset in range(0, len(pages), 4):
                        for items in pool.map(lambda p: self.collector.page_items(p['key']), pages[offset:offset+4]):
                            batch = []
                            for item in items:
                                search = ' '.join(str(item.get(k) or '') for k in ('symbol', 'display', 'underlying', 'security_id')).upper()
                                batch.append((position, item['key'], item.get('segment') or 'UNMAPPED', contract_expiry(item), search, json.dumps(item)))
                                position += 1
                            db.executemany('INSERT INTO instruments VALUES (?,?,?,?,?,?)', batch)
                db.execute('CREATE INDEX segment_expiry ON instruments(segment, expiry)')
                db.commit()
            os.replace(pending, target)
            for old in target.parent.glob('*.sqlite3'):
                if old != target:
                    old.unlink(missing_ok=True)
            return target, cat
        finally:
            pending.unlink(missing_ok=True)
            self.lock.release()

    @contextmanager
    def database(self):
        # Open under the publication lock. An already open SQLite reader remains
        # valid when a newer immutable master replaces its file on this Linux host.
        for _ in range(2):
            target, cat = self.index()
            with self.lock:
                if not target.is_file():
                    continue
                db = sqlite3.connect('file:'+str(target)+'?mode=ro', uri=True)
                break
        else:
            raise HTTPException(503, 'Catalogue changed during search; retry shortly')
        with closing(db):
            yield db, cat

    def search(self, query='', segment='', page=0, active_only=True):
        if len(query) > 80 or page < 0 or page > 100000 or segment not in ['', 'UNMAPPED', *SEGMENT_NAMES]:
            raise HTTPException(422, 'Invalid instrument search or page')
        where, args = ['1=1'], []
        if query.strip():
            literal = query.strip().upper().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            where.append("search LIKE ? ESCAPE '\\'")
            args.append('%' + literal + '%')
        if segment:
            where.append('segment=?'); args.append(segment)
        if active_only:
            where.append('(expiry IS NULL OR expiry>=?)'); args.append(datetime.now(IST).date().isoformat())
        clause = ' AND '.join(where)
        with self.database() as (db, cat):
            count = db.execute('SELECT count(*) FROM instruments WHERE '+clause, args).fetchone()[0]
            rows = db.execute('SELECT document FROM instruments WHERE '+clause+' ORDER BY position LIMIT 50 OFFSET ?', [*args, page*50]).fetchall()
        return {'catalogue_sha256': cat['sha256'], 'catalogue_at': cat.get('fetched_at'),
                'catalogue_rows': cat['rows'], 'matches': count, 'page': page,
                'next_page': page+1 if (page+1)*50 < count else None,
                'items': [json.loads(row[0]) for row in rows], 'segments': SEGMENT_NAMES,
                'review_reasons': cat.get('review_reasons', {}),
                'coverage_note': 'Catalogue discovery is not data coverage. Quotes/history require separate provider responses.'}

    def resolve(self, key, catalogue_sha256, current_quote=False):
        if not isinstance(key, str) or len(key) > 128:
            raise HTTPException(422, 'Invalid instrument key')
        with self.database() as (db, cat):
            if catalogue_sha256 != cat['sha256']:
                raise HTTPException(409, 'Instrument catalogue changed; search and select the instrument again')
            row = db.execute('SELECT document FROM instruments WHERE key=?', (key,)).fetchone()
        if not row:
            raise HTTPException(404, 'Instrument is not in the current catalogue')
        item = json.loads(row[0])
        if current_quote:
            try:age = (datetime.now(timezone.utc)-datetime.fromisoformat(cat['fetched_at'])).total_seconds()
            except (KeyError,TypeError,ValueError):age = float('inf')
            if not -5 <= age <= 86400:
                raise HTTPException(409, 'Catalogue is stale; refresh the full master before fetching current quotes')
        if item.get('reason') or item.get('segment') not in SEGMENT_NAMES or not str(item.get('security_id', '')).isdigit():
            raise HTTPException(409, 'Instrument is unavailable: '+str(item.get('reason') or 'exact segment / identity unresolved'))
        expiry = contract_expiry(item)
        if current_quote and expiry and expiry < datetime.now(IST).date().isoformat():
            raise HTTPException(409, 'Expired contract has no current quote; choose historical data instead')
        return item

    def recorded(self, key, catalogue_sha256):
        from data_system import LIVE_STREAMS, SCOPE
        from sdk_feeds import SEGMENTS as FEED_SEGMENTS
        item = self.resolve(key, catalogue_sha256)
        plan = self.collector.store.document(SCOPE)[0] or {}
        family = next((f for f in LIVE_STREAMS for i in plan.get('scopes', {}).get(f, [])
                       if i.get('segment') == item['segment'] and str(i.get('security_id')) == item['security_id']), None)
        result = {'instrument': item, 'catalogue_sha256': catalogue_sha256, 'recorder': family,
                  'checked_at': datetime.now(timezone.utc).isoformat(), 'observation': None,
                  'state': 'NOT_SUBSCRIBED', 'receipt_age_seconds': None,
                  'coverage_note': 'One retained observation does not certify continuous ticks or exchange quote freshness.'}
        if not family:
            return result
        latest = self.collector.store.document('system/latest/'+family)[0] or {}
        row = latest.get('rows', {}).get(str(FEED_SEGMENTS[item['segment']])+':'+item['security_id'])
        result.update(state='AWAITING_OBSERVATION', subscription_revision=plan.get('feed_revisions', {}).get(family, plan.get('revision')))
        if isinstance(row, dict) and row.get('segment') == item['segment'] and str(row.get('security_id')) == item['security_id']:
            result.update(state='RETAINED_OBSERVATION', observation=row)
            try:result['receipt_age_seconds'] = (datetime.now(timezone.utc)-datetime.fromisoformat(row['received_at'])).total_seconds()
            except (KeyError,TypeError,ValueError):pass
        return result

    async def quote(self, key, catalogue_sha256):
        item = await asyncio.to_thread(self.resolve, key, catalogue_sha256, True)
        with anyio.CancelScope(shield=True):
            owner = await anyio.to_thread.run_sync(self.collector.acquire)
        try:
            raw = await self.collector.call(owner, 'marketfeed/quote', {item['segment']: [int(item['security_id'])]})
            data = raw.get('data') if isinstance(raw, dict) else None
            values = data.get(item['segment'], {}) if isinstance(data, dict) else {}
            value = values.get(str(item['security_id']), {}) if isinstance(values, dict) else {}
            received = datetime.now(timezone.utc)
            identity = {**item, 'label': item.get('display') or item['symbol'], 'group': item['exchange'], 'expiry': contract_expiry(item)}
            result = quote_row(identity, value, received)
            result.update(observation=value, raw=raw, catalogue_sha256=catalogue_sha256)
            if item['instrument'] not in ('FUTIDX', 'FUTSTK', 'FUTCOM', 'FUTCUR'):
                result['note'] = 'Exact selected instrument; last-trade time is not exchange quote time'
            await asyncio.to_thread(self.collector.store.add, 'instrument_quote', result)
            return result
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.collector.release, owner)

    async def history(self, key, catalogue_sha256, start, end, family='intraday', interval=1):
        today = datetime.now(IST).date()
        if family not in ('daily', 'intraday') or interval not in (1, 5, 15, 25, 60):
            raise HTTPException(422, 'Choose daily or intraday data and a supported interval')
        if not date(1900, 1, 1) <= start < end <= today or (end-start).days > (366 if family == 'daily' else 31):
            raise HTTPException(422, 'Use completed dates: up to 31 days intraday or 366 days daily; To is exclusive')
        item = await asyncio.to_thread(self.resolve, key, catalogue_sha256)
        variant = {} if family == 'daily' else {'interval': str(interval)}
        task = {'family': family, 'item': item, 'variant': variant, 'from': start.isoformat(), 'to': end.isoformat()}
        body = {'securityId': str(item['security_id']), 'exchangeSegment': item['segment'],
                'instrument': item['instrument'], 'oi': item['instrument'] not in ('INDEX', 'EQUITY'),
                'fromDate': start.isoformat(), 'toDate': end.isoformat()}
        if family == 'daily':
            body['expiryCode'] = 0
        else:
            body.update(interval=str(interval), fromDate=start.isoformat()+' 00:00:00', toDate=end.isoformat()+' 00:00:00')
        with anyio.CancelScope(shield=True):
            owner = await anyio.to_thread.run_sync(self.collector.acquire)
        try:
            endpoint = 'charts/historical' if family == 'daily' else 'charts/intraday'
            raw = await self.collector.call(owner, endpoint, body)
            received = datetime.now(timezone.utc).isoformat()
            record = {'instrument': item, 'catalogue_sha256': catalogue_sha256, 'request': body,
                      'endpoint': endpoint, 'received_at': received, 'raw': raw}
            # Keep the source even if validation rejects it. Rejections do not
            # become completed normalized histories or advance a job checkpoint.
            await asyncio.to_thread(self.collector.store.add, 'instrument_history_source', record)
            quality = await asyncio.to_thread(self.collector.persist_candles, task, {'raw': raw})
            return {**record, 'quality': quality, 'coverage_note': 'Returned/validated rows are not proof of complete requested-session coverage.'}
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.collector.release, owner)
