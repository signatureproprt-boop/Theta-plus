#!/usr/bin/env bash
# Source-only update for the already configured Theta Plus Cloud Run service.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
project=theta-pluse
region=asia-south1
service=theta-plus
expected_version=theta-foundation-3.7.0
command -v gcloud >/dev/null
command -v curl >/dev/null
work=$(mktemp -d)
promoted=false
rollback=''
cleanup() {
  code=$?
  if [[ $code -ne 0 && $promoted == true ]]; then
    echo 'Smoke check failed; restoring original revision traffic.' >&2
    gcloud run services update-traffic "$service" --project="$project" --region="$region" --to-revisions="$rollback" --quiet || echo 'ROLLBACK FAILED: inspect Cloud Run immediately.' >&2
  fi
  rm -rf -- "$work"
  exit "$code"
}
trap cleanup EXIT
gcloud run services describe "$service" --project="$project" --region="$region" --format=json > "$work/before.json"
python3 - "$work/before.json" "$work" <<'PY'
import json,re,sys
from pathlib import Path
s=json.load(open(sys.argv[1]));dest=Path(sys.argv[2])
traffic={t['revisionName']:t['percent'] for t in s['status']['traffic'] if t.get('percent',0)>0}
assert sum(traffic.values())==100 and all(re.fullmatch(r'[a-z0-9-]+',k) for k in traffic),'Invalid traffic mapping'
(dest/'rollback').write_text(','.join(f'{k}={v}' for k,v in sorted(traffic.items())))
(dest/'url').write_text(s['status']['url'])
# This file can contain runtime configuration; never print or upload it.
PY
rollback=$(cat "$work/rollback")
url=$(cat "$work/url")
candidate=${1:-}
if [[ -n $candidate ]]; then
  [[ $candidate =~ ^theta-plus-f370-[0-9]{14}$ ]] || { echo 'Invalid candidate revision' >&2; exit 1; }
  echo "Reusing candidate $candidate."
else
  build_account="projects/$project/serviceAccounts/theta-plus-build@$project.iam.gserviceaccount.com"
  # Existing account only; no creation, IAM edits, secrets or configuration reset.
  gcloud iam service-accounts describe "theta-plus-build@$project.iam.gserviceaccount.com" --project="$project" --format='value(email)' >/dev/null
  suffix="f370-$(date -u +%Y%m%d%H%M%S)"
  candidate="$service-$suffix"
  echo "Building candidate $candidate; existing traffic remains unchanged."
  gcloud run deploy "$service" --source=. --project="$project" --region="$region" \
    --build-service-account="$build_account" --revision-suffix="$suffix" --no-traffic --quiet
fi
gcloud run revisions describe "$candidate" --project="$project" --region="$region" --format=json > "$work/candidate.json"
python3 - "$work/candidate.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))
assert any(c['type']=='Ready' and c['status']=='True' for c in r['status']['conditions']),'Candidate not ready'
PY
gcloud run services describe "$service" --project="$project" --region="$region" --format=json > "$work/current.json"
python3 - "$work/before.json" "$work/current.json" <<'PY'
import json,sys
def traffic(p):
 return {t['revisionName']:t['percent'] for t in json.load(open(p))['status']['traffic'] if t.get('percent',0)>0}
assert traffic(sys.argv[1])==traffic(sys.argv[2]),'Traffic changed during build; deployment stopped before promotion'
PY
promoted=true
gcloud run services update-traffic "$service" --project="$project" --region="$region" --to-revisions="$candidate=100" --quiet
# Poll through routing propagation, requiring the exact candidate identity.
python3 deploy/verify_rollout.py "$url" "$expected_version" "$candidate"
python3 deploy/verify_rollout.py --workspaces "$url"
curl --fail --silent --show-error --max-time 30 "$url/" > "$work/index.html"
python3 - "$work/index.html" <<'PY'
import sys
s=open(sys.argv[1]).read()
assert 'page-home' in s and 'page-admin' in s and 'page-collect' in s and 'Collect all data' in s,'Foundation UI missing'
assert 'page-room' not in s and 'Strategy lab' not in s,'Retired UI still present'
assert all('page-'+p in s for p in ('research','options','quality','risk','replay','paper')),'Research workspace missing'
PY
status=$(curl --silent --show-error --max-time 30 --output /dev/null --write-out '%{http_code}' "$url/api/status")
[[ $status == 200 ]] || { echo "Public API check failed: HTTP $status" >&2; exit 1; }
curl --fail --silent --show-error --max-time 30 "$url/api/status" > "$work/status.json"
python3 - "$work/status.json" <<'PYVERIFY'
import json,sys
s=json.load(open(sys.argv[1]))
assert s['auth_mode']=='public','Public mode mismatch'
PYVERIFY
admin_status=$(curl --silent --show-error --max-time 30 --output /dev/null --write-out '%{http_code}'   -H 'Content-Type: application/json' --data '{"token":"rollout-probe-not-a-real-token"}' "$url/api/dhan/token")
[[ $admin_status == 403 || $admin_status == 503 ]] || { echo "Token admin protection failed: HTTP $admin_status" >&2; exit 1; }
retired_status=$(curl --silent --show-error --max-time 30 --output /dev/null --write-out '%{http_code}' "$url/api/overview")
[[ $retired_status == 410 ]] || { echo "Old analysis still available: HTTP $retired_status" >&2; exit 1; }
curl --fail --silent --show-error --max-time 30 "$url/api/dhan/collected" > "$work/retained-data.json"
collection_status=$(curl --silent --show-error --max-time 30 --output /dev/null --write-out '%{http_code}' -H 'Content-Type: application/json' --data '{}' "$url/api/collection/catalogue/refresh")
[[ $collection_status == 403 || $collection_status == 503 ]] || { echo 'Collection Admin protection failed' >&2; exit 1; }
python3 deploy/sync_worker_image.py "$work/candidate.json"
promoted=false
printf '\nDEPLOY VERIFIED\nVersion: %s\nRevision: %s\nURL: %s\n' "$expected_version" "$candidate" "$url"
printf 'Rollback if needed: gcloud run services update-traffic %s --project=%s --region=%s --to-revisions=%s\n' "$service" "$project" "$region" "$rollback"
