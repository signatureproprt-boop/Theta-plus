#!/usr/bin/env bash
# One-time Cloud Shell setup. No token is read or printed by this script.
set +x
set -euo pipefail
THETA_PROJECT=theta-pluse
THETA_REGION=asia-south1
THETA_SECRET=theta-dhan-access-token
gcloud secrets describe "$THETA_SECRET" --project="$THETA_PROJECT" >/dev/null
THETA_SA=$(gcloud run services describe theta-plus --project="$THETA_PROJECT" --region="$THETA_REGION" --format='value(spec.template.spec.serviceAccountName)')
[[ -n "$THETA_SA" ]] || { echo 'Cloud Run service account missing'; exit 1; }
gcloud secrets add-iam-policy-binding "$THETA_SECRET" --project="$THETA_PROJECT" \
  --member="serviceAccount:$THETA_SA" --role=roles/secretmanager.secretAccessor >/dev/null
gcloud secrets add-iam-policy-binding "$THETA_SECRET" --project="$THETA_PROJECT" \
  --member="serviceAccount:$THETA_SA" --role=roles/secretmanager.secretVersionAdder >/dev/null
gcloud run services update theta-plus --project="$THETA_PROJECT" --region="$THETA_REGION" \
  --update-env-vars="THETA_DHAN_TOKEN_SECRET=projects/$THETA_PROJECT/secrets/$THETA_SECRET" >/dev/null
echo 'Dhan token screen enabled. Client ID stays fixed in existing server secret.'
