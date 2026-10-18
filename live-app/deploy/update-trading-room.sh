#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
THETA_PROJECT=theta-pluse
THETA_REGION=asia-south1
THETA_URL=$(gcloud run services describe theta-plus --project="$THETA_PROJECT" --region="$THETA_REGION" --format='value(status.url)')
# Update source only. Keep existing Dhan secrets, workspace key, storage and IAM.
gcloud run deploy theta-plus --source=. --project="$THETA_PROJECT" --region="$THETA_REGION" --timeout=1200 \
  --build-service-account="projects/$THETA_PROJECT/serviceAccounts/theta-plus-build@$THETA_PROJECT.iam.gserviceaccount.com"
THETA_CHECK=$(mktemp)
trap 'rm -f "$THETA_CHECK"' EXIT
curl --fail --silent --show-error "$THETA_URL/" > "$THETA_CHECK"
python3 - "$THETA_CHECK" <<'PY'
import sys
from pathlib import Path
assert 'id="page-room"' in Path(sys.argv[1]).read_text(), 'Trading room was not found in deployed HTML'
print('Trading room HTML deployment verified.')
PY
THETA_STATUS=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "$THETA_URL/api/markets")
[[ "$THETA_STATUS" == 401 ]] || { echo 'API protection check failed'; exit 1; }
printf '\nOpen and refresh: %s\n' "$THETA_URL"
echo 'Unlock workspace, then open Trading room. Existing Dhan credentials are preserved.'
