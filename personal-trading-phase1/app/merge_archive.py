"""Idempotent import of the owner's GCS mirror, with byte verification.

No source deletion, guessed mappings or live activation. Run on the dedicated VM.
"""
import argparse
import hashlib
import json
from pathlib import Path
from sqlalchemy import select, func, text
from sqlalchemy.dialects.postgresql import insert
from app.db import session
from app.legacy_store import valid_path, PostgresStore
from app.models import LegacyObject, Instrument, MasterIdentity, Watchlist, SystemState, FetchJob, utcnow
from app.market_collection import put_state


def import_file(db, path, raw):
    valid_path(path)
    sha = hashlib.sha256(raw).hexdigest()
    row = db.get(LegacyObject, path)
    if row:
        if row.sha256 == sha:
            return 'unchanged'
        if row.sha256 != row.source_sha256:
            raise ValueError('LOCAL_ARCHIVE_CONFLICT:' + path)
        row.content, row.sha256, row.source_sha256 = raw, sha, sha
        row.version += 1
        return 'updated'
    db.add(LegacyObject(path=path, content=raw, sha256=sha, source_sha256=sha, version=1))
    return 'inserted'


def normalize_candles(db, name, raw):
    """Retain raw bytes regardless; promote only exact, understood OHLCV series."""
    if not name.startswith('collection/candles/') or not name.endswith('.json'):
        return {'state': 'NOT_CANDLE_SERIES'}
    from datetime import date, timedelta
    from decimal import Decimal
    from app import dhan
    from app.policy import validate_identity
    from app.tasks import save_rows
    source = json.loads(raw)
    item, variant = source['instrument'], source['variant']
    family = 'daily' if variant == {} else 'intraday' if set(variant) == {'interval'} else None
    if family is None:
        return {'state': 'RETAINED_SEPARATE_ROLLING_SERIES'}
    expected = {'exchange': item['exchange'], 'segment': item['segment'], 'sid': item['security_id'],
        'instrument': item['instrument'], 'family': family, 'variant': variant}
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if source['series'] != digest or name != 'collection/candles/'+digest+'/'+source['day']+'.json':
        raise ValueError('SERIES_IDENTITY_MISMATCH')
    validate_identity(item['segment'], item['instrument'])
    identity = (item['segment'], str(item['security_id']))
    instrument, meta = db.get(Instrument, identity), db.get(MasterIdentity, identity)
    if (not instrument or not instrument.mapping_verified or not meta or meta.blocked or
        instrument.instrument_type != item['instrument'] or
        (item.get('symbol') and instrument.symbol.upper() != item['symbol'].upper()) or
        (item.get('expiry') and item['instrument'] not in ('INDEX', 'EQUITY') and
         (not meta.expiry or meta.expiry.isoformat() != item['expiry']))):
        return {'state': 'ARCHIVED_IDENTITY_REVIEW'}
    day = date.fromisoformat(source['day'])
    interval = '1d' if family == 'daily' else str(variant['interval'])
    if interval not in ('1d', '1', '5', '15', '25', '60'):
        raise ValueError('UNSUPPORTED_INTERVAL')
    candles = list(source['candles'].values())
    body = {key: [r[key] for r in candles] for key in ('timestamp', 'open', 'high', 'low', 'close', 'volume')}
    for key in ('open', 'high', 'low', 'close'):
        body[key] = [str(Decimal(str(value)).quantize(Decimal('0.00000001'))) for value in body[key]]
    rows = dhan.parse_candles(body, day, day+timedelta(days=1))
    key = dict(exchange_segment=identity[0], security_id=identity[1], interval=interval,
               start_date=day, end_date=day+timedelta(days=1), expiry_code=0)
    db.execute(insert(FetchJob).values(**key, status='MIGRATED_SOURCE').on_conflict_do_nothing())
    job = db.scalar(select(FetchJob).where(*(getattr(FetchJob, k)==v for k,v in key.items())))
    added, conflicts = save_rows(db, job, rows)
    return {'state': 'NORMALIZED' if not conflicts else 'SAVED_CANDLE_CONFLICT',
        'inserted_rows': added, 'conflicting_rows': conflicts}


def import_archive(root):
    root = Path(root).resolve(strict=True)
    manifest_path = root / 'migration-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('source') != 'gs://theta-pluse-theta-plus-data' or not isinstance(manifest.get('objects'), list):
        raise ValueError('INVALID_MIGRATION_MANIFEST')
    counts = {'inserted': 0, 'updated': 0, 'unchanged': 0}
    total_bytes = 0
    normalized = {'inserted_rows': 0, 'conflicting_rows': 0, 'review_objects': 0}
    review = []
    with session() as db:
        db.execute(text('SELECT pg_advisory_lock(73462003)'))
        db.commit()
        try:
            put_state(db, 'legacy-migration', {'state': 'IMPORTING', 'checked_at': utcnow().isoformat()})
            db.commit()
            for entry in manifest['objects']:
                name = valid_path(entry['path'])
                path = root / name
                if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
                    raise ValueError('UNSAFE_IMPORT_PATH')
                if path.stat().st_size > 256 * 1024 ** 2:
                    raise ValueError('OBJECT_TOO_LARGE_REVIEW_REQUIRED')
                raw = path.read_bytes()
                if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                    raise ValueError('ARCHIVE_CHECKSUM_MISMATCH:' + name)
                if db.scalar(text('SELECT pg_database_size(current_database())')) >= 20 * 1024**3:
                    raise ValueError('STORAGE_SOFT_LIMIT')
                counts[import_file(db, name, raw)] += 1
                total_bytes += len(raw)
                if name.startswith('collection/candles/'):
                    try:
                        with db.begin_nested():
                            result = normalize_candles(db, name, raw)
                        normalized['inserted_rows'] += result.get('inserted_rows', 0)
                        normalized['conflicting_rows'] += result.get('conflicting_rows', 0)
                        if result['state'] not in ('NORMALIZED', 'RETAINED_SEPARATE_ROLLING_SERIES'):
                            normalized['review_objects'] += 1
                            if len(review) < 100: review.append({'path': name, 'state': result['state']})
                    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                        normalized['review_objects'] += 1
                        if len(review) < 100: review.append({'path': name, 'state': type(exc).__name__})
                db.commit()  # Resume safely after interruption.
            # Verify destination bytes independently after all writes.
            for entry in manifest['objects']:
                row = db.get(LegacyObject, entry['path'])
                if row is None or hashlib.sha256(row.content).hexdigest() != entry['sha256']:
                    raise ValueError('DESTINATION_VERIFICATION_FAILED')
            receipt = {'state': 'VERIFIED', 'source': manifest['source'],
                'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                'objects': len(manifest['objects']), 'bytes': total_bytes, 'counts': counts,
                'checked_at': utcnow().isoformat(), 'source_deleted': False,
                'native_ohlcv': normalized, 'review': review,
                'normalization_precision': '8 decimal places; source bytes unchanged',
                'normalized_history_coverage_verified': False}
            put_state(db, 'legacy-migration', receipt)
            db.commit()
            return receipt
        except Exception as exc:
            db.rollback()
            put_state(db, 'legacy-migration', {'state': 'REVIEW_REQUIRED',
                'checked_at': utcnow().isoformat(), 'error_type': type(exc).__name__, 'counts': counts})
            db.commit()
            raise
        finally:
            db.execute(text('SELECT pg_advisory_unlock(73462003)'))
            db.commit()


def activate(legacy_stopped):
    if not legacy_stopped:
        raise ValueError('CONFIRM_LEGACY_COLLECTOR_STOPPED_FIRST')
    with session() as db:
        state = db.get(SystemState, 'legacy-migration')
        if not state or state.data.get('state') != 'VERIFIED':
            raise ValueError('VERIFY_ARCHIVE_IMPORT_FIRST')
        plan = PostgresStore().document('system/subscription-plan')[0] or {}
        defaults = []
        for family in ('live', 'live-extra', 'live-3', 'live-4'):
            defaults.extend(plan.get('scopes', {}).get(family, []))
        if not defaults:
            defaults = [{'segment': 'IDX_I', 'security_id': sid, 'instrument': 'INDEX'}
                        for sid in ('13', '25', '21')]
        seeded, held, expired = 0, 0, 0
        today = utcnow().astimezone(__import__('app.dhan', fromlist=['IST']).IST).date()
        for old in defaults:
            identity = (old.get('segment'), str(old.get('security_id')))
            if (identity[0] or '').startswith('MCX') or old.get('instrument') in ('FUTCOM', 'OPTFUT'):
                held += 1
                continue
            item, meta = db.get(Instrument, identity), db.get(MasterIdentity, identity)
            if meta and meta.expiry and meta.expiry < today:
                expired += 1
                continue
            if (not item or not meta or not item.mapping_verified or meta.blocked or
                    item.instrument_type != old.get('instrument') or
                    (old.get('symbol') and item.symbol.upper() != old['symbol'].upper()) or
                    (old.get('expiry') and item.instrument_type not in ('INDEX', 'EQUITY') and
                     (not meta.expiry or meta.expiry.isoformat() != old['expiry']))):
                raise ValueError('OLD_RECORDING_IDENTITY_NEEDS_REVIEW:' + ':'.join(identity))
            db.execute(insert(Watchlist).values(exchange_segment=identity[0], security_id=identity[1])
                       .on_conflict_do_nothing())
            seeded += 1
        from app.config import settings
        if db.scalar(select(func.count()).select_from(Watchlist)) > settings().live_connections*5000:
            raise ValueError('RECORDING_CAPACITY_EXCEEDED')
        if not db.scalar(select(func.count()).select_from(Watchlist)):
            raise ValueError('VERIFIED_WATCHLIST_REQUIRED')
        put_state(db, 'collection-owner', {'state': 'NEW_BACKEND', 'checked_at': utcnow().isoformat(),
            'legacy_stop_attested': True, 'archive_manifest_sha256': state.data['manifest_sha256'],
            'retained_recording_identities': seeded, 'held_commodities': held, 'expired_not_subscribed': expired, 'mcx': 'HOLD'})
        db.commit()
    return {'owner': 'NEW_BACKEND', 'live': 'WILL_START_FROM_VERIFIED_WATCHLIST'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    archive = sub.add_parser('import')
    archive.add_argument('directory')
    enable = sub.add_parser('activate')
    enable.add_argument('--confirm-legacy-stopped', action='store_true')
    sub.add_parser('deactivate')
    args = parser.parse_args()
    if args.command == 'deactivate':
        with session() as db:
            put_state(db, 'collection-owner', {'state': 'HOLD', 'checked_at': utcnow().isoformat()})
            db.commit()
        print('{"owner":"HOLD"}')
        raise SystemExit(0)
    print(json.dumps(import_archive(args.directory) if args.command == 'import'
                     else activate(args.confirm_legacy_stopped)))
