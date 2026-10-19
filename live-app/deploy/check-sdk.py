"""Confirm SDK and worker configuration; does not start an unbounded backfill."""
import json
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import time
import urllib.request

BASE='https://theta-plus-jw4jpkebxa-el.a.run.app'

def get(path):
    with urllib.request.urlopen(BASE+path,timeout=45) as response:return json.load(response)

def main():
    for attempt in range(12):
        try:
            health=get('/health-check');engine=get('/api/collection/engine')
            if health.get('version')=='theta-foundation-3.6.0' and engine['sdk']['version']=='2.3.0' and engine['worker']['configured']:
                print('VERIFIED: Theta Plus 3.6.0, official DhanHQ-py 2.3.0, private background worker configured.')
                print('Open Collect all data, select your plan and Resume. The server continues after the tablet closes.')
                print('SDK installation does not establish historical coverage; inspect outcomes and coverage separately.')
                day=datetime.now(ZoneInfo('Asia/Kolkata')).date()-timedelta(days=1)
                while day.weekday()>=5:day-=timedelta(days=1)
                body={'kind':'daily','index':'nifty','start':str(day),'end':str(day+timedelta(days=1))}
                try:
                    req=urllib.request.Request(BASE+'/api/dhan/history',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,timeout=100) as response:record=json.load(response)
                    if record.get('sdk_version')=='2.3.0' and record.get('rows',0)>0:
                        print('LIVE SDK FETCH VERIFIED:',record['rows'],'NIFTY daily rows saved; record',record['id'])
                        return 0
                except Exception:pass
                print('PENDING: SDK/worker configured, but the live data fetch did not verify. Check Dhan token, dates and entitlement.')
                return 2
        except Exception:pass
        time.sleep(5)
    print('PENDING: SDK/worker rollout could not be verified. Check Cloud Run service and job.')
    return 2

if __name__=='__main__':raise SystemExit(main())
