# Collector repair — 3.7.1

This release corrects the official SDK's marketfeed wire payload, derives chain
underlyings from actual option contracts, and keeps partial/missing results visible.
It does not certify provider availability or complete historical coverage.

## Preserved state

Stored raw responses, normalized rows and job checkpoints remain intact. Legacy
chain jobs migrate only at an underlying boundary, retaining all previous outcomes.
Snapshot retries request missing identities only. Historical review probes stay
within the original request dates. Authentication, permission and quota failures
retain the current checkpoint.

## Deploy and activate

Run `bash live-app/deploy/release-collector.sh` from the repository checkout in
the existing authenticated Cloud Shell. The service rollout retains the existing
health, workspace and Admin protection checks and synchronizes the collector image.
Activation then cancels only executions of theta-plus-collector, confirms their
termination, waits 610 seconds for durable leases, rechecks the immutable image,
and requests a fresh execution. It never clears leases or edits stored data.

If activation fails, the successfully verified web release remains deployed;
inspect the explicit error before retrying activation. Do not assume the worker
has changed just because the web revision is healthy.

## Acceptance in production

- Confirm a fresh worker heartbeat and that history checkpoints advance.
- Compare one exact-ID quote with its retained raw bulk and isolated responses.
- Keep empty and partial results unresolved until the provider returns data.
- Inspect option expiry and chain results for index, stock and MCX underlyings.
- Diagnose representative failed historical instruments and review returned bounds.
- Report actual earliest/latest timestamps, gaps, failures and unsupported segments.

NSE_M has no verified API segment mapping and remains explicitly unresolved.
An instrument master entry is discovery, not proof of historical or live coverage.
The compact Dhan-style design preview is a separate change and is not deployed
by this collector repair.
