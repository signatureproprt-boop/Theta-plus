"""Old Theta Plus Store interface backed by the SAME PostgreSQL database.

The import retains source bytes. Normal compatibility writes use CAS, never SQLite.
"""
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from app.db import session
from app.models import LegacyObject

MAX_ID = 9007199254740991


def valid_path(path):
    if (not path or len(path) > 1024 or path.startswith('/') or
        any(p in ('', '.', '..') for p in path.split('/')) or
        not re.fullmatch(r'[A-Za-z0-9_./-]+', path)):
        raise ValueError('INVALID_ARCHIVE_PATH')
    return path


def encoded(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':')).encode()


class PostgresStore:
    @staticmethod
    def document_key(name):
        return valid_path('collection/' + name + '.json')

    def read(self, path):
        with session() as db:
            row = db.get(LegacyObject, valid_path(path))
            if row is None:
                return None
            return json.loads(row.content)

    def document(self, name):
        with session() as db:
            row = db.get(LegacyObject, self.document_key(name))
            return (json.loads(row.content), row.version) if row else (None, 0)

    def put_document(self, name, payload, version):
        raw = encoded(payload)
        key = self.document_key(name)
        values = dict(content=raw, sha256=hashlib.sha256(raw).hexdigest())
        with session() as db:
            if version == 0:
                result = db.execute(insert(LegacyObject).values(path=key, version=1, **values)
                    .on_conflict_do_nothing().returning(LegacyObject.path)).first()
            else:
                result = db.execute(update(LegacyObject).where(LegacyObject.path == key,
                    LegacyObject.version == version).values(version=version + 1, **values)
                    .returning(LegacyObject.path)).first()
            db.commit()
            return result is not None

    def names(self, prefix, after='', limit=100):
        with session() as db:
            return list(db.scalars(select(LegacyObject.path).where(
                LegacyObject.path.startswith(prefix, autoescape=True), LegacyObject.path > after
            ).order_by(LegacyObject.path).limit(limit)))

    def document_names(self, prefix, after='', limit=100):
        root = 'collection/'
        return [p[len(root):-5] for p in self.names(root + prefix,
            root + after + '.json' if after else '', limit) if p.endswith('.json')]

    def job_documents(self):
        after = ''
        while True:
            names = self.document_names('jobs/', after, 100)
            if not names:
                break
            for name in names:
                yield self.document(name)[0]
            after = names[-1]

    def job_inventory_stats(self):
        return {'storage': 'postgresql', 'generation_checked': True}

    def write_once(self, path, payload):
        raw = encoded(payload)
        with session() as db:
            row = db.execute(insert(LegacyObject).values(path=valid_path(path), content=raw,
                sha256=hashlib.sha256(raw).hexdigest(), version=1).on_conflict_do_nothing()
                .returning(LegacyObject.path)).first()
            db.commit()
            return row is not None

    def add(self, kind, payload):
        if not re.fullmatch('[a-zA-Z0-9_]+', kind):
            raise ValueError('INVALID_KIND')
        for _ in range(10):
            rid = int(time.time() * 1000) * 1000 + secrets.randbelow(1000)
            if self.write_once(f'records/{kind}/{MAX_ID-rid:016d}.json',
                    {'id': rid, 'created_at': datetime.now(timezone.utc).isoformat(), 'data': payload}):
                return rid
        raise RuntimeError('RECORD_ID_CONTENTION')

    def get(self, kind, rid):
        if not re.fullmatch('[a-zA-Z0-9_]+', kind):
            raise ValueError('INVALID_KIND')
        return self.read(f'records/{kind}/{MAX_ID-int(rid):016d}.json')

    def rows(self, kind, limit=100):
        return [self.read(p) for p in self.names('records/' + valid_path(kind) + '/', limit=limit)]

    def journal_add(self, plan_id, payload):
        import uuid
        key = str(uuid.UUID(str(plan_id)))
        return self.write_once('journal/' + key + '.json', {**payload, 'plan_id': key,
            'created_at': datetime.now(timezone.utc).isoformat()})

    def journal_rows(self):
        rows = [self.read(p) for p in self.names('journal/', limit=1000)]
        return sorted(rows, key=lambda r: r.get('created_at', ''), reverse=True)

    def diagnostic_names(self, namespace, after='', limit=25):
        root = {'documents': 'collection/', 'records': 'records/', 'journal': 'journal/'}[namespace]
        return [p[len(root):-5] for p in self.names(root, root + after + '.json' if after else '', limit)
                if p.endswith('.json')]

    def diagnostic_read(self, namespace, key):
        if namespace == 'documents':
            return self.document(key)[0]
        root = {'records': 'records/', 'journal': 'journal/'}[namespace]
        row = self.read(root + key + '.json')
        return {**row, 'kind': key.split('/')[0]} if row and namespace == 'records' else row

    def put_catalogue_source(self, sha, source):
        from pathlib import Path
        if not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('INVALID_SHA')
        raw = Path(source).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('SOURCE_CHECKSUM_MISMATCH')
        with session() as db:
            db.execute(insert(LegacyObject).values(path=f'collection/catalogue/{sha}/source.csv',
                content=raw, sha256=sha, version=1).on_conflict_do_nothing())
            db.commit()

    def live_quotes(self, items):
        """Read only requested identities, not the whole 20,000-symbol hash."""
        from app.rate_gate import redis_client
        from app.models import Instrument, MasterIdentity
        from datetime import datetime, timezone
        import math
        values = redis_client().hmget('theta:live:latest',
            [i.get('segment','')+':'+str(i.get('security_id','')) for i in items]) if items else []
        keys = [i.get('segment','')+':'+str(i.get('security_id','')) for i in items]
        previous_values = redis_client().hmget('theta:live:previous', keys) if keys else []
        result = {}
        with session() as db:
            for item, raw, previous_raw in zip(items, values, previous_values):
                if not raw: continue
                row = json.loads(raw)
                price = row.get('ltp')
                if row.get('kind') != 'tick' or not isinstance(price, (float,int)) or not math.isfinite(price) or price <= 0:
                    continue
                identity = (item.get('segment'), str(item.get('security_id')))
                instrument, meta = db.get(Instrument, identity), db.get(MasterIdentity, identity)
                if (not instrument or not instrument.mapping_verified or not meta or meta.blocked or
                    instrument.instrument_type != item.get('instrument') or
                    (item.get('expiry') and instrument.instrument_type not in ('INDEX','EQUITY') and
                     (not meta.expiry or meta.expiry.isoformat() != item['expiry']))):
                    continue
                result[item['key']] = {'price': price, 'volume': row.get('cumulative_volume'),
                    'oi': None, 'received_at': row['received_at'], 'trade_at': row.get('trade_time'),
                    'state': 'RETAINED_TRADE' if row.get('trade_time') else 'TIME_UNVERIFIED',
                    'time_basis': 'Dhan last trade and local receipt; exchange quote freshness not certified'}
                previous = float(previous_raw) if previous_raw else row.get('previous_close')
                if previous and isinstance(previous, (int,float)) and math.isfinite(previous) and previous > 0:
                    result[item['key']].update(previous_close=previous, change=price-previous,
                        change_pct=100*(price-previous)/previous)
        return result

    def native_candle_document(self, item, series, day, variant, source=None):
        from datetime import date, datetime, time, timedelta
        from app import dhan
        from app.models import Instrument, MasterIdentity, HistoricalData
        start = datetime.combine(date.fromisoformat(day), time(), tzinfo=dhan.IST)
        with session() as db:
            identity = (item.get('segment'), str(item.get('security_id')))
            instrument, meta = db.get(Instrument, identity), db.get(MasterIdentity, identity)
            if (not instrument or not meta or meta.blocked or not instrument.mapping_verified or
                instrument.instrument_type != item.get('instrument') or
                (item.get('expiry') and instrument.instrument_type not in ('INDEX','EQUITY') and
                 (not meta.expiry or meta.expiry.isoformat() != item['expiry']))):
                return source
            rows = db.scalars(select(HistoricalData).where(HistoricalData.exchange_segment == identity[0],
                HistoricalData.security_id == identity[1], HistoricalData.interval == str(variant.get('interval','1d')),
                HistoricalData.timestamp >= start, HistoricalData.timestamp < start+timedelta(days=1))).all()
            if not rows: return source
            candles = dict((source or {}).get('candles', {}))
            for row in rows:
                stamp = int(row.timestamp.timestamp())
                # Existing archived source rows retain precedence; native rows fill absences.
                candles.setdefault(str(stamp), {'timestamp': stamp,
                    **{k:float(getattr(row,k)) for k in ('open','high','low','close')}, 'volume':row.volume})
            return {'series':series, 'day':day, 'instrument':item, 'variant':variant, 'candles':candles}
