from datetime import datetime, timedelta, timezone
from app.index_analysis import analyse_tick

NOW = datetime(2026, 10, 27, 4, 30, tzinfo=timezone.utc)


def test_fresh_requires_both_receipt_and_exchange_time():
    tick = {'kind': 'tick', 'ltp': 100.0}
    assert analyse_tick(tick, NOW, NOW, NOW)['state'] == 'FRESH'
    assert analyse_tick(tick, NOW, NOW-timedelta(seconds=31), NOW)['state'] == 'STALE_OR_INVALID_TIME'
    assert analyse_tick(tick, NOW, NOW+timedelta(seconds=1), NOW)['state'] == 'STALE_OR_INVALID_TIME'
    assert analyse_tick(tick, NOW, None, NOW)['state'] == 'TIME_UNVERIFIED'


def test_no_false_live_for_holiday_or_bad_price():
    tick = {'kind': 'tick', 'ltp': 100.0}
    holiday = datetime(2026, 10, 20, 4, 30, tzinfo=timezone.utc)
    assert analyse_tick(tick, holiday, holiday, holiday)['state'] == 'MARKET_CLOSED'
    assert analyse_tick({'kind': 'tick', 'ltp': float('nan')}, NOW, NOW, NOW)['state'] == 'INVALID_PRICE'
    assert analyse_tick(None, None, None, NOW)['state'] == 'NO_DATA'
