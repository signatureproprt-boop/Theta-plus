"""Pinned NSE 2026 regular-session evidence; other calendars stay unverified."""
NSE_2026 = {'2026-' + day for day in ('01-26', '03-03', '03-26', '03-31', '04-03',
    '04-14', '05-01', '05-28', '06-26', '09-14', '10-02', '10-20', '11-10', '11-24', '12-25')}
SOURCE = 'https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf'


def session(day, exchange):
    if exchange != 'NSE' or day.year != 2026:
        return {'state': 'CALENDAR_UNVERIFIED', 'expected_minutes': None}
    if day.isoformat() == '2026-11-08':
        return {'state': 'SPECIAL_SESSION_TIMES_UNVERIFIED', 'expected_minutes': None}
    closed = day.weekday() >= 5 or day.isoformat() in NSE_2026
    return {'state': 'CLOSED' if closed else 'REGULAR', 'expected_minutes': 0 if closed else 375, 'source': SOURCE}
