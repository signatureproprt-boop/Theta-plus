#!/usr/bin/env bash
# Intended for interactive Google Cloud Shell. No secrets requested in chat.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
command -v gcloud >/dev/null || { echo 'Run this in Google Cloud Shell.'; exit 1; }
THETA_STATE=.theta-cloud-project
if [[ -s "$THETA_STATE" ]]; then
  read -r THETA_PROJECT < "$THETA_STATE"
  [[ "$THETA_PROJECT" =~ ^theta-plus-[a-z0-9-]+$ ]] || { echo 'Invalid saved project ID'; exit 1; }
  gcloud projects describe "$THETA_PROJECT" >/dev/null
  printf 'Resuming project: %s\n' "$THETA_PROJECT"
else
  echo 'Your available billing accounts:'
  gcloud billing accounts list --filter='open=true' --format='table(name.basename():label=ACCOUNT_ID,displayName:label=NAME)'
  echo 'Google usage may incur charges on the account you select.'
  read -r -p 'Enter the billing ACCOUNT_ID from the list: ' THETA_BILLING
  [[ "$THETA_BILLING" =~ ^[A-Fa-f0-9]{6}-[A-Fa-f0-9]{6}-[A-Fa-f0-9]{6}$ ]] || { echo 'No valid billing account selected; nothing created.'; exit 1; }
  gcloud billing accounts describe "$THETA_BILLING" >/dev/null
  THETA_PROJECT="theta-plus-$(date +%y%m%d)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
  gcloud projects create "$THETA_PROJECT" --name='Theta Plus'
  printf '%s\n' "$THETA_PROJECT" > "$THETA_STATE"
  printf '%s\n' "$THETA_BILLING" > .theta-cloud-billing
fi
if [[ $(gcloud billing projects describe "$THETA_PROJECT" --format='value(billingEnabled)') != True ]]; then
  if [[ -s .theta-cloud-billing ]]; then
    read -r THETA_BILLING < .theta-cloud-billing
  else
    gcloud billing accounts list --filter='open=true'
    read -r -p 'Billing ACCOUNT_ID: ' THETA_BILLING
  fi
  [[ "$THETA_BILLING" =~ ^[A-Fa-f0-9]{6}-[A-Fa-f0-9]{6}-[A-Fa-f0-9]{6}$ ]] || exit 1
  gcloud billing projects link "$THETA_PROJECT" --billing-account="$THETA_BILLING"
fi
bash deploy/google-cloud.sh "$THETA_PROJECT"
