#!/usr/bin/env bash
set +x
set -euo pipefail
THETA_PROJECT=theta-pluse
THETA_REGION=asia-south1
THETA_SA=$(gcloud run services describe theta-plus --project="$THETA_PROJECT" --region="$THETA_REGION" --format='value(spec.template.spec.serviceAccountName)')
[[ -n "$THETA_SA" ]] || { echo 'Runtime account not found'; exit 1; }
read -r -p 'Dhan Client ID: ' THETA_CLIENT </dev/tty
read -r -s -p 'Dhan Access Token (paste, then Enter): ' THETA_TOKEN </dev/tty
printf '\n'
[[ -n "$THETA_CLIENT" && -n "$THETA_TOKEN" ]] || { echo 'Both fields are required'; exit 1; }
for THETA_SECRET in theta-dhan-client-id theta-dhan-access-token; do
  if ! gcloud secrets describe "$THETA_SECRET" --project="$THETA_PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$THETA_SECRET" --replication-policy=automatic --project="$THETA_PROJECT" >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$THETA_SECRET" --project="$THETA_PROJECT" --member="serviceAccount:$THETA_SA" --role=roles/secretmanager.secretAccessor >/dev/null
done
THETA_CV=$(printf '%s' "$THETA_CLIENT" | gcloud secrets versions add theta-dhan-client-id --data-file=- --project="$THETA_PROJECT" --format='value(name)')
THETA_TV=$(printf '%s' "$THETA_TOKEN" | gcloud secrets versions add theta-dhan-access-token --data-file=- --project="$THETA_PROJECT" --format='value(name)')
unset THETA_CLIENT THETA_TOKEN
gcloud run services update theta-plus --project="$THETA_PROJECT" --region="$THETA_REGION" --update-secrets="DHAN_CLIENT_ID=theta-dhan-client-id:${THETA_CV##*/},DHAN_ACCESS_TOKEN=theta-dhan-access-token:${THETA_TV##*/}"
echo 'Credentials attached. Refresh the app and check Data connections; live data access still needs verification.'
