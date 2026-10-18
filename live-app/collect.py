"""Run a checkpointed collection job without keeping the dashboard open.

Run in an authenticated terminal you control. This process must remain running;
the server's durable cursor survives a stopped runner. No password is saved.
"""
import argparse
import getpass
import json
import time
import urllib.error
import urllib.parse
import urllib.request

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--job',required=True,help='Saved job ID from Collect all data')
    args=parser.parse_args()
    url=args.url.rstrip('/')
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use the HTTPS app URL, without credentials or a query')
    if len(args.job)!=32 or any(c not in '0123456789abcdef' for c in args.job):
        parser.error('Use the 32-character saved job ID')
    password=getpass.getpass('Existing Theta Plus Admin password: ')
    def post(action):
        request=urllib.request.Request(url+'/api/collection/jobs/'+args.job+'/'+action,
            data=b'{}',headers={'Content-Type':'application/json','X-Theta-Admin-Password':password})
        # Refuse redirects so the password cannot be forwarded to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**k):return None
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=240) as response:
            return json.load(response)
    try:
        job=post('resume')
        print('Resuming job',args.job)
        while job['state']=='running':
            try:job=post('run')
            except urllib.error.HTTPError as exc:
                if exc.code in (409,429,502,503,504):
                    print('Request busy/unavailable; retrying saved checkpoint.')
                    time.sleep(20);continue
                raise
            print(job['state'],json.dumps(job['counts'],sort_keys=True),job['items_assessed'],'items')
            if job['state']=='running':time.sleep(3.5)
        print('Stopped:',job['state'],job.get('message',''))
    except KeyboardInterrupt:
        try:post('pause')
        except Exception:pass
        print('\nStopped. Checkpoint retained; run again to resume.')
    except (urllib.error.URLError,ValueError):
        print('Runner stopped because the server request failed. Check the app and saved checkpoint; no password was printed.')
        return 1
    return 0

if __name__=='__main__':
    raise SystemExit(main())
