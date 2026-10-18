# Google Cloud deployment

Status: prepared and locally tested, NOT deployed. Cloud console was unavailable
in the assistant's browser. No project ID, billing state or cloud permissions
have been verified. No cloud resources have been created.

## Architecture

Cloud Run serves the tablet UI and API over HTTPS. A private Cloud Storage
bucket stores immutable JSON records, datasets, journal and experiment results.
The app never places SQLite on Cloud Run's ephemeral disk. Local development
continues to use SQLite. Existing local records are NOT automatically migrated.

The HTML shell is public; every data API requires the private workspace key.
Secret Manager stores that key. The runtime service account only receives
object access on the app bucket and access to that one secret. Broker tokens
must be added separately as secrets; no token is included in the build.

## First deployment (Google Cloud Shell)

1. Select a billing-enabled Google Cloud project. Confirm this is the Theta Plus
   project. Do not assume an unrelated CRM project is the intended target.
2. Upload and extract theta-plus-app.zip. Open the app directory in Cloud Shell.
3. Review deploy/google-cloud.sh, then run:

   bash deploy/google-cloud.sh YOUR_PROJECT_ID

The script enables APIs, creates a dedicated runtime identity and private bucket,
creates a workspace key if absent, builds and deploys the app in asia-south1,
sets the actual allowed hostname, and checks HTML availability plus unauthenticated
API rejection. Source deployment also requires the Google Cloud Build service
account to have the documented build permissions in the project. If an IAM step
fails, fix the named missing permission; do not grant broad Owner access as a shortcut.

The script prints the HTTPS app URL. In Secret Manager, view the
`theta-plus-workspace-key` secret and enter it in the app's workspace-key field.
Never send this secret to chat. Verify an authenticated dataset import and saved
journal before using the deployment for research.

## Collection and costs

Default: minimum zero instances, maximum one, request-based CPU. This is NOT an
always-on collector. The open overview/analysis screen requests refresh about
every 30 seconds in weekday session hours. Browser suspension, tab closure and
network loss interrupt collection; gaps must remain visible. Imported historical
data is required for longer reliable backtests. No synthetic history is supplied.

Google may charge for builds, storage, requests and runtime. No free-cost or
monthly-price guarantee is made. Continuous collection requires a separate
operational decision, instance-based CPU/minimum instances and a distributed
collector lease to prevent duplicate polling during revision overlap. It is not
enabled by this deployment script.

## Live Dhan connection

Create private Secret Manager secrets for DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN,
grant only the Theta runtime account access, and attach them to the Cloud Run
revision under those exact environment-variable names. Import current exact
contract metadata in Data connections. The application does not send orders.
Live provider authentication, permissions and real quote behaviour remain untested.

## Operational limitations

This is a single-user research application, not a validated trading system.
GCS durability tests use a fake bucket; a real Google integration and tablet
browser smoke test are still required. Background collection is not guaranteed.
Records accumulate; no retention policy deletes your history automatically.
Listing all journal objects and loading large datasets should be monitored before
scaling to many users. No automated migration from local SQLite is included.

## Official documentation

- https://docs.cloud.google.com/run/docs/deploying-source-code
- https://docs.cloud.google.com/run/docs/container-contract
- https://docs.cloud.google.com/run/docs/configuring/services/secrets
- https://docs.cloud.google.com/storage/docs/request-preconditions

## Create a new project automatically

Run `bash deploy/new-project.sh` in Cloud Shell from the extracted app directory.
It lists available open billing accounts and asks which account to use. It creates
a unique `theta-plus-...` project, links that billing account, and deploys.
If no billing account exists, create one through Google's billing UI first.
The script needs permission to create projects and link billing accounts.

A dedicated build service account receives `roles/run.builder` only in this
project. IAM grants can take a few minutes to propagate. If deployment fails,
rerun `bash deploy/new-project.sh` from the SAME directory; the saved project ID
is reused instead of creating another project. Do not re-extract into another
folder to retry. The script has been syntax-checked, but real cloud execution
remains unverified.
