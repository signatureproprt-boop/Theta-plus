"""Run in Google Cloud Shell. Prints diagnostics, never secret values."""
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone

PROJECT='theta-pluse'
REGION='asia-south1'

def cloud(*args):
    proc=subprocess.run(['gcloud',*args,'--project='+PROJECT],capture_output=True,text=True)
    if proc.returncode:
        raise RuntimeError('Google command failed. Check Cloud Shell login and project permissions; no credentials printed.')
    return proc.stdout.strip()

def main():
    svc=json.loads(cloud('run','services','describe','theta-plus','--region='+REGION,'--format=json'))
    url=svc['status']['url']
    env={r['name']:r for r in svc['spec']['template']['spec']['containers'][0].get('env',[])}
    print('SERVICE:',url)
    print('READY_REVISION:',svc['status'].get('latestReadyRevisionName'))
    print('AUTO_REFRESH_SETTING:',env.get('THETA_REQUEST_REFRESH',{}).get('value','not set'))
    ref=env.get('THETA_API_KEY',{}).get('valueFrom',{}).get('secretKeyRef',{})
    if not ref:raise RuntimeError('Workspace key secret binding missing; stop and inspect service configuration.')
    key=cloud('secrets','versions','access',ref['key'],'--secret='+ref['name'])
    def api(path,post=False):
        req=urllib.request.Request(url+'/api/'+path,data=b'{}' if post else None,
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=180) as resp:return resp.status,json.load(resp)
        except urllib.error.HTTPError as exc:
            try:body=json.load(exc)
            except Exception:body={}
            detail=body.get('detail','')
            # Only show server messages from known fixed application error families.
            safe=detail if isinstance(detail,str) and detail.startswith(('Dhan ','Provider ','Workspace ','Host ','Cross-origin ','A refresh','No active','Intraday ','Candle ','Instrument ')) else 'Request failed; inspect server logs privately'
            return exc.code,{'detail':safe}
    code,status=api('status')
    print('STATUS_HTTP:',code)
    if code!=200:
        print('ERROR:',status.get('detail'));return
    print('DHAN_CONFIGURED:',status.get('dhan_configured'))
    print('AUTO_REFRESH_NOW:',status.get('request_refresh'))
    code,result=api('refresh',True)
    print('PROVIDER_FETCH_HTTP:',code)
    if code!=200:print('PROVIDER_ERROR:',result.get('detail'))
    code,overview=api('overview')
    print('OVERVIEW_HTTP:',code)
    print('DATA_STATE:',overview.get('status'))
    at=overview.get('timestamp')
    print('LAST_FETCH_UTC:',at)
    if at:
        print('FETCH_AGE_SECONDS:',round((datetime.now(timezone.utc)-datetime.fromisoformat(at)).total_seconds()))
    print('COMPLETED_5MIN_BARS:',len(overview.get('bars',[])))
    contracts=overview.get('contracts',[])
    print('OPTION_CONTRACTS:',len(contracts))
    now=datetime.now(timezone.utc)
    recent=sum(1 for r in contracts if r.get('quote_at') and 0<=(now-datetime.fromisoformat(r['quote_at'])).total_seconds()<=60)
    print('CONTRACTS_WITH_RECENT_TRADE_TIME:',recent)
    print('CONTRACTS_WITH_LOT_SIZE:',sum(1 for r in contracts if r.get('lot_size')))
    code,_=api('markets')
    print('TOOLBAR_API_HTTP:',code)
    print('END: no tokens or workspace keys printed.')

if __name__=='__main__':
    try:main()
    except (RuntimeError,KeyError,ValueError,urllib.error.URLError,TimeoutError) as exc:
        print('CHECK_FAILED:',str(exc) if isinstance(exc,RuntimeError) else 'Connection or response failed; retry after checking Cloud Shell login.')
        raise SystemExit(1)
