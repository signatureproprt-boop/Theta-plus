"""Versioned exchange evidence, never a weekday-only completeness claim."""
from datetime import datetime,timedelta
from runtime import IST

NSE_2026={'2026-'+day for day in ('01-26','03-03','03-26','03-31','04-03','04-14','05-01','05-28','06-26','09-14','10-02','10-20','11-10','11-24','12-25')}
SOURCE='https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf'

def session(day,exchange='NSE'):
    if exchange!='NSE' or day.year!=2026:
        return {'state':'CALENDAR_UNVERIFIED','expected_minutes':None,'source':None}
    if day.isoformat()=='2026-11-08':
        return {'state':'SPECIAL_SESSION_TIMES_UNVERIFIED','expected_minutes':None,'source':SOURCE}
    closed=day.weekday()>=5 or day.isoformat() in NSE_2026
    return {'state':'CLOSED' if closed else 'REGULAR','expected_minutes':0 if closed else 375,'source':SOURCE}

def recent_closed_day(clock):
    local=clock.astimezone(IST)
    # Wait until 02:00 IST; MCX can trade close to midnight. Late provider
    # publication is handled by overlapping subsequent historical requests.
    day=local.date()-timedelta(days=1 if local.hour>=2 else 2)
    while day.weekday()>=5 and day.isoformat()!='2026-11-08':day-=timedelta(days=1)
    return day

def trading_state(clock,exchange='NSE'):
    local=clock.astimezone(IST);result=session(local.date(),exchange)
    if result['state']!='REGULAR':return result['state']
    return 'OPEN' if '09:15'<=local.strftime('%H:%M')<'15:30' else 'CLOSED'
