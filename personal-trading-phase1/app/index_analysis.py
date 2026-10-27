"""Read-only index snapshots from persisted ticks; stale data is never labelled live."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo
from app.calendar_evidence import session

IST = ZoneInfo('Asia/Kolkata')


def analyse_tick(payload, received_at, trade_at, now):
    """Return freshness evidence; no trading signal is inferred from a single tick."""
    calendar = session(now.astimezone(IST).date(), 'NSE')
    local = now.astimezone(IST)
    opened = calendar['state'] == 'REGULAR' and (9, 15) <= (local.hour, local.minute) < (15, 30)
    result = {'session': 'OPEN' if opened else calendar['state'] if calendar['state'] != 'REGULAR' else 'CLOSED',
              'state': 'NO_DATA', 'price': None, 'received_at': received_at, 'trade_at': trade_at,
              'trend': 'INSUFFICIENT_DATA', 'freshness_seconds': None}
    if not isinstance(payload, dict) or payload.get('kind') != 'tick':
        return result
    value = payload.get('ltp')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
        result['state'] = 'INVALID_PRICE'
        return result
    result['price'] = value
    if not received_at or received_at.tzinfo is None or not trade_at or trade_at.tzinfo is None:
        result['state'] = 'TIME_UNVERIFIED'
        return result
    receipt_age = (now - received_at).total_seconds()
    trade_age = (now - trade_at).total_seconds()
    result['freshness_seconds'] = max(receipt_age, trade_age)
    if not opened:
        result['state'] = 'MARKET_CLOSED' if calendar['state'] in ('REGULAR', 'CLOSED') else 'CALENDAR_UNVERIFIED'
    elif 0 <= receipt_age <= 30 and 0 <= trade_age <= 30:
        result['state'] = 'FRESH'
    else:
        result['state'] = 'STALE_OR_INVALID_TIME'
    return result


def install(app, protected):
    # All identities are catalogue records. A watchlist flag does not imply a live subscription.
    from fastapi import HTTPException
    from sqlalchemy import text
    from app.db import session as db_session

    @app.get('/analysis/indices', dependencies=protected)
    def indices():
        query = text('''
            SELECT i.exchange_segment, i.security_id, i.symbol,
                   (w.security_id IS NOT NULL) AS in_watchlist,
                   t.timestamp AS received_at, t.trade_time, t.payload
            FROM instruments i
            JOIN master_identities m ON m.exchange_segment=i.exchange_segment AND m.security_id=i.security_id
            LEFT JOIN watchlist w ON w.exchange_segment=i.exchange_segment AND w.security_id=i.security_id
            LEFT JOIN LATERAL (
                SELECT timestamp, trade_time, payload FROM live_ticks
                WHERE exchange_segment=i.exchange_segment AND security_id=i.security_id
                  AND payload->>'kind'='tick'
                ORDER BY timestamp DESC LIMIT 1
            ) t ON true
            WHERE i.instrument_type='INDEX' AND i.exchange_segment='IDX_I'
              AND i.mapping_verified IS TRUE AND m.blocked IS FALSE AND m.exchange='NSE'
            ORDER BY i.symbol, i.security_id LIMIT 1000
        ''')
        now = datetime.now(timezone.utc)
        with db_session() as db:
            rows = db.execute(query).mappings().all()
        items = []
        for row in rows:
            item = {key: row[key] for key in ('exchange_segment', 'security_id', 'symbol', 'in_watchlist')}
            item.update(analyse_tick(row['payload'], row['received_at'], row['trade_time'], now))
            items.append(item)
        return {'checked_at': now, 'items': items, 'scope': 'Verified NSE index identities in local database; watchlist is not proof of subscription',
                'coverage_verified': False, 'trading_signal': False, 'mcx': 'HOLD'}
