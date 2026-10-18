import os
from pathlib import Path
import uvicorn

# Deliberately simple KEY=VALUE loader, no eval, interpolation or shell execution.
env=Path(__file__).parent/'.env'
if env.exists():
    for line in env.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key,value=line.split('=',1)
            os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))

if __name__=='__main__':
    host=os.environ.get('THETA_HOST','127.0.0.1')
    if host not in ('127.0.0.1','localhost') and not os.environ.get('THETA_API_KEY'):
        raise SystemExit('A non-local listener requires THETA_API_KEY and THETA_ALLOWED_HOSTS. Use HTTPS at your reverse proxy.')
    uvicorn.run('server:app',host=host,port=int(os.environ.get('PORT',os.environ.get('THETA_PORT','8080'))),workers=1,proxy_headers=True,forwarded_allow_ips='*' if os.environ.get('K_SERVICE') else '127.0.0.1')
