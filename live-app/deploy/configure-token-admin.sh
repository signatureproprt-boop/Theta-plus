#!/usr/bin/env bash
# One-time configuration only; never reads or prints the Dhan token or Client ID.
set +x
set -euo pipefail
project=theta-pluse
region=asia-south1
service=theta-plus
read -r -p 'Google Web OAuth Client ID (.apps.googleusercontent.com): ' web_client </dev/tty
read -r -p 'Authorised Google admin email: ' admin_email </dev/tty
[[ $web_client =~ ^[a-zA-Z0-9.-]+\.apps\.googleusercontent\.com$ ]] || { echo 'Invalid Google Web Client ID'; exit 1; }
[[ $admin_email =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]] || { echo 'Invalid admin email'; exit 1; }
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
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
gcloud run services update "$service" --project="$project" --region="$region" \
  --update-env-vars="^|^THETA_GOOGLE_CLIENT_ID=$web_client|THETA_ADMIN_EMAILS=$admin_email|THETA_DHAN_TOKEN_SECRET=$secret" --quiet
echo 'Admin configuration saved. Permanent Dhan Client ID unchanged. Deploy the token-admin release next.'
