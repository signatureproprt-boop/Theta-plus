#!/usr/bin/env bash
# One-time configuration only; never reads or prints the Dhan token or Client ID.
set +x
set -euo pipefail
project=theta-pluse
region=asia-south1
service=theta-plus
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# The password is entered only in the terminal; only its salted hash is saved.
python3 - "$work/admin-hash" <<'PYHASH'
import getpass,hashlib,secrets,sys
from pathlib import Path
print('Password input is hidden. Type 12+ English letters/numbers, then press Enter.',flush=True)
for attempt in range(5):
    try:
        password=getpass.getpass('Choose token-admin password: ')
        if not 12<=len(password)<=512:
            print(f'Received {len(password)} characters. Need 12–512. Please try again.',flush=True)
            continue
        if not password.isascii() or not all(32<=ord(c)<127 for c in password):
            print('Use English keyboard: letters, numbers or standard symbols. Please try again.',flush=True)
            continue
        if password!=getpass.getpass('Repeat the same password: '):
            print('The two entries differ. Please try again.',flush=True)
            continue
        break
    except (EOFError,KeyboardInterrupt):
        raise SystemExit('Password entry cancelled; configuration was not changed.') from None
else:
    raise SystemExit('Password not accepted after 5 attempts; configuration was not changed.')
salt=secrets.token_hex(32)
digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
Path(sys.argv[1]).write_text(f'pbkdf2_sha256$600000${salt}${digest}')
PYHASH
chmod 600 "$work/admin-hash"
gcloud run services describe "$service" --project="$project" --region="$region" --format=json > "$work/service.json"
python3 - "$work/service.json" "$work" "$project" <<'PY'
import json,sys,re
from pathlib import Path
s=json.load(open(sys.argv[1]));dest=Path(sys.argv[2]);project=sys.argv[3]
spec=s['spec']['template']['spec'];env={e['name']:e for e in spec['containers'][0].get('env',[])}
assert 'DHAN_CLIENT_ID' in env,'Permanent Client ID must be configured first'
secret=env.get('THETA_DHAN_TOKEN_SECRET',{}).get('value','')
if not secret:
 name=env.get('DHAN_ACCESS_TOKEN',{}).get('valueFrom',{}).get('secretKeyRef',{}).get('name','')
 assert name,'No existing token Secret Manager reference found; stop before changing configuration'
 secret=name if name.startswith('projects/') else f'projects/{project}/secrets/{name}'
assert re.fullmatch(r'projects/[a-zA-Z0-9-]+/secrets/[a-zA-Z0-9_-]+',secret),'Unexpected secret reference'
assert spec.get('serviceAccountName'),'Runtime account missing'
(dest/'secret').write_text(secret)
(dest/'account').write_text(spec['serviceAccountName'])
PY
secret=$(cat "$work/secret")
account=$(cat "$work/account")
gcloud secrets describe "$secret" --project="$project" >/dev/null
for role in roles/secretmanager.secretAccessor roles/secretmanager.secretVersionAdder; do
  gcloud secrets add-iam-policy-binding "$secret" --project="$project" --member="serviceAccount:$account" --role="$role" >/dev/null
done
admin_secret=theta-token-admin-password-hash
if ! gcloud secrets describe "$admin_secret" --project="$project" >/dev/null 2>&1; then
  gcloud secrets create "$admin_secret" --project="$project" --replication-policy=automatic >/dev/null
fi
admin_version=$(gcloud secrets versions add "$admin_secret" --project="$project" --data-file="$work/admin-hash" --format='value(name)')
gcloud secrets add-iam-policy-binding "$admin_secret" --project="$project" --member="serviceAccount:$account" --role=roles/secretmanager.secretAccessor >/dev/null
gcloud run services update "$service" --project="$project" --region="$region" \
  --update-env-vars="THETA_DHAN_TOKEN_SECRET=$secret" \
  --update-secrets="THETA_TOKEN_ADMIN_HASH=$admin_secret:${admin_version##*/}" --quiet
echo 'Token admin configured. Dhan Client ID unchanged. Deploy the token-admin release next.'
