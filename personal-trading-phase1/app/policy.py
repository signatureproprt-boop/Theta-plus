import random
from datetime import date, timedelta

TYPES = {
    'IDX_I': {'INDEX'}, 'NSE_EQ': {'EQUITY'}, 'BSE_EQ': {'EQUITY'},
    'NSE_FNO': {'FUTIDX', 'FUTSTK', 'OPTIDX', 'OPTSTK'},
    'BSE_FNO': {'FUTIDX', 'FUTSTK', 'OPTIDX', 'OPTSTK'},
    'NSE_CURRENCY': {'FUTCUR', 'OPTCUR'}, 'BSE_CURRENCY': {'FUTCUR', 'OPTCUR'},
}
INTERVALS = {'1d', '1', '5', '15', '25', '60'}


def validate_identity(segment, instrument_type):
    # Immutable Phase 1 policy. There is deliberately no MCX enable switch.
    if segment.startswith('MCX') or instrument_type in {'FUTCOM', 'OPTFUT'}:
        raise ValueError('COMMODITY_HOLD')
    if instrument_type not in TYPES.get(segment, set()):
        raise ValueError('NEEDS_MAPPING')


def windows(start: date, end: date):
    while start < end:
        stop = min(start + timedelta(days=90), end)
        yield start, stop
        start = stop


def retry_delay(attempt, base, cap, retry_after=None):
    # Equal jitter; every delay has a nonzero lower bound.
    ceiling = min(cap, base * 2 ** (attempt - 1))
    delay = random.randint(max(1, ceiling // 2), ceiling)
    return max(delay, retry_after or 0)
