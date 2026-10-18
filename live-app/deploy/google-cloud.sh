#!/usr/bin/env bash
# Run in Google Cloud Shell from app/. Requires a billing-enabled project.
set -euo pipefail
THETA_PROJECT=${1:?Usage: bash deploy/google-cloud.sh GOOGLE_PROJECT_ID}
[[ "$THETA_PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo 'Invalid project ID'; exit 1; }
THETA_REGION=${THETA_REGION:-asia-south1}
THETA_SERVICE=theta-plus
THETA_ACCOUNT="theta-plus-runtime@${THETA_PROJECT}.iam.gserviceaccount.com"
THETA_BUCKET="${THETA_PROJECT}-theta-plus-data"
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
command -v gcloud >/dev/null
# No global project configuration is changed.
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com iam.googleapis.com --project="$THETA_PROJECT"
if ! gcloud iam service-accounts describe "$THETA_ACCOUNT" --project="$THETA_PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create theta-plus-runtime --display-name='Theta Plus runtime' --project="$THETA_PROJECT"
fi
if ! gcloud storage buckets describe "gs://$THETA_BUCKET" --project="$THETA_PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$THETA_BUCKET" --location="$THETA_REGION" --uniform-bucket-level-access --public-access-prevention --project="$THETA_PROJECT"
fi
gcloud storage buckets add-iam-policy-binding "gs://$THETA_BUCKET" --member="serviceAccount:$THETA_ACCOUNT" --role=roles/storage.objectUser --project="$THETA_PROJECT" >/dev/null
if ! gcloud secrets describe theta-plus-workspace-key --project="$THETA_PROJECT" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | gcloud secrets create theta-plus-workspace-key --data-file=- --replication-policy=automatic --project="$THETA_PROJECT"
fi
gcloud secrets add-iam-policy-binding theta-plus-workspace-key --member="serviceAccount:$THETA_ACCOUNT" --role=roles/secretmanager.secretAccessor --project="$THETA_PROJECT" >/dev/null
# Dedicated build identity works on new projects without a default Compute account.
THETA_BUILD_ACCOUNT="theta-plus-build@${THETA_PROJECT}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$THETA_BUILD_ACCOUNT" --project="$THETA_PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create theta-plus-build --display-name='Theta Plus source build' --project="$THETA_PROJECT"
fi
gcloud projects add-iam-policy-binding "$THETA_PROJECT" --member="serviceAccount:$THETA_BUILD_ACCOUNT" --role=roles/run.builder --condition=None >/dev/null
# Scale-to-zero is intentional until continuous collection is explicitly enabled.
# Workspace key protects all data APIs; only the HTML shell is public.
gcloud run deploy "$THETA_SERVICE" --source=. --project="$THETA_PROJECT" --region="$THETA_REGION" \
  --build-service-account="projects/$THETA_PROJECT/serviceAccounts/$THETA_BUILD_ACCOUNT" \
  --service-account="$THETA_ACCOUNT" --allow-unauthenticated --memory=1Gi --cpu=1 \
  --min-instances=0 --max-instances=1 --concurrency=8 --timeout=300 \
  --update-env-vars="THETA_HOST=0.0.0.0,THETA_GCS_BUCKET=$THETA_BUCKET,THETA_ALLOWED_HOSTS=localhost,THETA_REQUEST_REFRESH=true" \
  --update-secrets=THETA_API_KEY=theta-plus-workspace-key:latest
THETA_URL=$(gcloud run services describe "$THETA_SERVICE" --project="$THETA_PROJECT" --region="$THETA_REGION" --format='value(status.url)')
THETA_HOSTNAME=${THETA_URL#https://}
gcloud run services update "$THETA_SERVICE" --project="$THETA_PROJECT" --region="$THETA_REGION" --update-env-vars="THETA_ALLOWED_HOSTS=$THETA_HOSTNAME" >/dev/null
curl --fail --silent --show-error "$THETA_URL/" >/dev/null
THETA_STATUS=$(curl --silent --output /dev/null --write-out '%{http_code}' "$THETA_URL/api/status")
[[ "$THETA_STATUS" == 401 ]] || { echo 'API authentication check failed'; exit 1; }
printf '\nTheta Plus URL: %s\n' "$THETA_URL"
echo 'Open Secret Manager > theta-plus-workspace-key to obtain your private workspace key. Do not paste it into chat.'
