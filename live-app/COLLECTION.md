# Dhan collection release 3.1

## What this adds
The Collect all data page creates durable plans against a versioned copy of the
full Dhan instrument master. All discovered rows are retained, including unknown
segment mappings and invalid/duplicate entries, with review reasons.

Families: daily history, intraday history, rolling expired options, active-expiry
chains, quote batches, live-feed samples and 20/200-level depth samples.
Intraday/rolling intervals: 1, 5, 15, 25 and 60 minutes. Index rolling plans probe
WEEK/MONTH and near/next/far; near index strikes reach ATM ±10, other cases ±3.
Actual entitlement and valid combinations are established by provider outcomes.

Daily requests use the selected date range. Minute history uses ≤90-day windows;
rolling uses ≤30 days. Dates outside the documented five-year minute/rolling
horizon become explicit unsupported work items. The current catalogue cannot
resolve every retired futures contract or establish exact historical inception.
An old start date is a request boundary, not proof of complete inception coverage.

## Storage and recovery
SQLite documents use transactional compare-and-swap. GCS documents use generation
preconditions and bypass immutable-record caching. A shared 10-minute lease
serializes collection requests across instances. Every request executes one
checkpointed work item; it never relies on a background thread staying alive.
Unfinished leases expire after a process dies. A pause wins over in-flight results.

Raw historical responses are keyed by the full request identity and reused on
repeated plans. Normalized day/series documents merge by timestamp (and actual
strike for rolling data), so overlapping fetched windows do not multiply candles.
Daily and intraday series are distinct. Empty/failed outcomes are visible; Retry
creates a child job with a fresh raw-response identity and retains the old evidence.
Permission failures stop with the current item intact; record-and-skip preserves
the reason and allows other coverage to proceed. Token failures preserve the
cursor until the existing Admin token screen updates credentials and Resume runs.

All collection writes require the existing Admin password. The password remains
in browser memory or the runner's memory and is never stored in the job. The
original records/archive are retained. No order or trading API is added.

## Scope boundaries
- A completed job means each selected work item has an outcome, including failed,
  empty or unsupported outcomes. It is not a declaration that all market data is
  available or all market sessions are complete.
- A full exchange holiday/special-session calendar is not bundled. Normalized
  history marks calendar validation pending; missing candles are never filled.
- Quote batches contain up to 1,000 instruments; omitted security IDs are reported.
  Timestamp freshness must be evaluated separately from receipt time.
- Live feed batches contain up to 100 instruments, depth20 up to 50, depth200 one.
  Each work item records at most ten seconds or 1,000 received frames. Headers,
  packet types and requested security IDs are inspected; raw binary is preserved
  in base64. Exchange price/timestamp payloads are not decoded in this release.
  Instruments with no matching frame are listed. This is not continuous recording.
- Live/depth sample times are current receipt times, independent of the historical
  date range. No historical depth, historical tick feed or past full chain is
  manufactured.
- Unsupported mappings (including NSE_M when absent from the documented API enum)
  remain in the catalogue and coverage results. International markets require a
  separately verified source.
- Collection is sequential under a shared lease. REST calls reserve a persisted
  daily budget and at least 3.2-second spacing within this collector. Legacy manual
  fetch endpoints retain their existing independent provider throttle; avoid
  running them in parallel with a bulk collector.
- Full-universe plans may be extremely large and incur runtime/storage/request
  charges. All intervals/families are selectable; a blank symbol/segment filter
  means the full discovered universe.

## Run
1. Deploy live-app with deploy/release-existing.sh from an authenticated Cloud
   Shell. It builds a candidate without traffic, retains existing environment,
   secrets, storage and IAM, verifies readiness, promotes and rolls back on smoke
   failure. Version: theta-foundation-3.1.1, candidate suffix f311.
2. Open Admin and unlock with the existing Admin password.
3. Open Collect all data, check subscription/segments, refresh the full catalogue.
4. Choose dates, families and intervals. Create plan, then Resume selected job.
5. Keep the page open. Closing or leaving it stops new requests; Resume continues
   the saved cursor. Refresh progress and View results expose outcomes/exports.
6. To drive a saved job without the browser, run:
   python3 collect.py --url https://YOUR-APP.run.app --job JOB_ID
   Enter the existing Admin password at the private terminal prompt. Leave this
   process running. A stopped process can resume the same job later. No unattended
   scheduler, paid always-on resource or new credential is installed.

## Validation
Local Python tests exercise local and mocked GCS CAS, cross-instance lease
exclusion, restart recovery, overlapping-window deduplication, token and permission
pauses, explicit empty/failed results, retry, pagination, window/strike bounds,
all-expiry chain checkpoints, quote omissions, and Admin protections.
No real Dhan credentials are used in tests. Real segment/depth entitlements,
Cloud Run storage writes and a production fetch must be checked after deployment.

Rejected requests (DH-905 or HTTP 400/422) now stop in `request_review` at
the same item without automatic retry or advancing coverage. A manual Resume
retries that exact task; changed dates or filters require a new plan. Existing
job configurations and checkpoints are preserved. The default start date for
new plans is five years ago, and new plans visit index pages first. Before a
full plan, verify a short daily-history range for one symbol. Catalogue rows
include expired contracts; inclusion does not prove historical API eligibility.
`provider_calls` counts fetch attempts, including rejected requests (and sample
connection attempts), excluding raw-cache reuse and quota-denied work.
