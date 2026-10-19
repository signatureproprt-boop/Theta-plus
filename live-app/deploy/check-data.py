"""Verify the recent saved dataset and daily endpoint after deployment."""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

BASE='https://theta-plus-jw4jpkebxa-el.a.run.app'

def request(path,body=None):
    req=urllib.request.Request(BASE+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=180) as response:return json.load(response)


def main():
    today=datetime.now(ZoneInfo('Asia/Kolkata')).date();start=today;days=0
    while days<5:
        start-=timedelta(days=1)
        if start.weekday()<5:days+=1
    health=request('/health-check')
    if health.get('version')!='theta-foundation-3.4.0':
        print('PENDING: live version is',health.get('version'));return 2
    print('LIVE VERSION VERIFIED:',health['version'],flush=True)
    report=request(f'/api/dhan/coverage?start={start}&end={today}')
    expected={('index',i,None) for i in ('nifty','banknifty','india_vix')}|{('expired_option',i,s) for i in ('nifty','banknifty') for s in ('CALL','PUT')}
    complete=set()
    for g in report['groups']:
        key=(g['kind'],g['index'],g['side'])
        # The recovered options sample is monthly ATM expiry code 1.
        target=g['kind']=='index' or (g['strike']=='ATM' and g['expiry_flag']=='MONTH' and g['expiry_code']==1)
        if target and g['weekday_grid_complete']:complete.add(key)
        print(g['index'],g['side'] or 'index',g['rows'],'regular rows;',sum(s['missing'] for s in g['sessions']),'missing;',g['conflicting_rows'],'conflicts',flush=True)
    pending=expected-complete
    print('RECENT COVERAGE:', 'WEEKDAY GRID VERIFIED' if not pending else 'PENDING '+str(sorted(pending)),flush=True)
    print(report['calendar_validation'],report['options'],flush=True)
    last_session=today-timedelta(days=1)
    while last_session.weekday()>=5:last_session-=timedelta(days=1)
    daily_end=last_session+timedelta(days=1)
    for index in ('nifty','banknifty','india_vix'):
        try:
            result=request('/api/dhan/history',{'kind':'daily','index':index,'start':str(start),'end':str(daily_end)})
            if result.get('rows',0)>0 and result.get('quality',{}).get('status')=='ROWS_PRESENT':
                print('DAILY',index,result['rows'],'rows saved',flush=True)
            else:
                pending.add(('daily',index,None));print('PENDING DAILY',index,'empty/invalid response',flush=True)
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode()[:800]
            pending.add(('daily',index,None));print('PENDING DAILY',index,detail,flush=True)
        except (urllib.error.URLError,TimeoutError):
            pending.add(('daily',index,None));print('PENDING DAILY',index,'connection failed',flush=True)
    print('RAW DATA RETAINED. No invented candles. Fixed-contract backtest validation remains pending.',flush=True)
    print('CHECKS FINISHED:', 'PENDING ITEMS REMAIN' if pending else 'RECENT ACQUISITION AND WEEKDAY GRID VERIFIED',flush=True)
    return 2 if pending else 0

if __name__=='__main__':
    try:sys.exit(main())
    except (urllib.error.URLError,ValueError,TimeoutError) as exc:
        print('PENDING: live check could not finish:',type(exc).__name__);sys.exit(2)
