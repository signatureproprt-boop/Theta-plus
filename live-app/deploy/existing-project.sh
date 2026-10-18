#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
THETA_PROJECT=${1:?Pass your existing Google Cloud project ID}
[[ "$THETA_PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || exit 1
gcloud projects describe "$THETA_PROJECT" --format='table(projectId,name)'
THETA_BILLING_ENABLED=$(gcloud billing projects describe "$THETA_PROJECT" --format='value(billingEnabled)')
if [[ "${THETA_BILLING_ENABLED,,}" != true ]]; then
  gcloud billing accounts list --filter='open=true' --format='table(name.basename():label=ACCOUNT_ID,displayName:label=NAME)'
  echo 'Google usage may incur charges on the billing account you select.'
  read -r -p 'Enter billing ACCOUNT_ID from the list: ' THETA_BILLING
  [[ "$THETA_BILLING" =~ ^[A-Fa-f0-9]{6}-[A-Fa-f0-9]{6}-[A-Fa-f0-9]{6}$ ]] || { echo 'Invalid billing account ID'; exit 1; }
  gcloud billing projects link "$THETA_PROJECT" --billing-account="$THETA_BILLING"
fi
bash deploy/google-cloud.sh "$THETA_PROJECT"
