"""Requested-session evidence; never fills missing candles or guesses calendars."""
from datetime import date,timedelta,datetime,timezone
from data_calendar import session
from runtime import IST


def cache_is_closed(task,response):
    """An archive captured before its requested range closed is not final."""
    try:
        received=datetime.fromisoformat(response['received_at'])
        if received.tzinfo is None:return False
        # Same conservative cutoff as unattended EOD scheduling, including MCX.
        closed=datetime.fromisoformat(task['to']).replace(tzinfo=IST)+timedelta(hours=2)
        return received>=closed
    except (KeyError,ValueError,TypeError):return False


def requested_grid(task,by_day):
    item=task['item'];family=task['family']
    supported=(item.get('exchange')=='NSE' and item.get('segment') in ('IDX_I','NSE_EQ','NSE_FNO')
               and (family=='daily' or str(task.get('variant',{}).get('interval'))=='1'))
    if not supported:return {'coverage':'CALENDAR_OR_INTERVAL_UNVERIFIED','calendar_verified':False}
    day=date.fromisoformat(task['from']);end=date.fromisoformat(task['to'])
    expected=0;unknown=0;missing_days=[];missing_minutes=0
    while day<end:
        calendar=session(day)
        if calendar['state']=='REGULAR':
            expected+=1
            rows=by_day.get(day.isoformat(),{}).values()
            if family=='daily':
                present=bool(by_day.get(day.isoformat()))
            else:
                minutes=set()
                for row in rows:
                    at=datetime.fromtimestamp(row['timestamp'],timezone.utc).astimezone(IST)
                    minute=at.hour*60+at.minute
                    if 555<=minute<930:minutes.add(minute)
                present=bool(minutes)
                missing_minutes+=375-len(minutes)
            if not present:missing_days.append(day.isoformat())
        elif calendar['state']!='CLOSED':unknown+=1
        day+=timedelta(days=1)
    gaps=bool(missing_days or missing_minutes)
    complete=not unknown and not gaps and bool(expected)
    return {'coverage':'PARTIAL_SESSION_GRID' if gaps else 'SESSION_GRID_COMPLETE' if complete else 'CALENDAR_UNVERIFIED' if unknown else 'NO_REGULAR_SESSIONS',
            'coverage_basis':'Validated rows returned for the exact request; no absent series or exchange-time freshness certification',
            'calendar_verified':not unknown,'unverified_calendar_days':unknown,
            'expected_sessions':expected,'missing_sessions':len(missing_days),'missing_session_dates':missing_days[:31],
            'missing_minutes':missing_minutes if family!='daily' else None,'session_grid_complete':complete}
