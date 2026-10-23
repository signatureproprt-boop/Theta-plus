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
   failure. Version: theta-foundation-3.1.3, candidate suffix f313.
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

Index and equity instruments do not use contract expiry dates. Placeholder
expiry values such as 0001-01-01 are ignored both at import and when reading
existing saved catalogue pages. No catalogue refresh or deletion is required.
Old zero-work completed jobs remain as evidence; create a new plan to collect
their intended range. New jobs assessing zero work stop as no_work rather than
claiming successful completion.


### Saved-data validation (3.1.3)
- Minute requests begin at IST midnight so the exact 09:15 opening minute is
  included. Historical requests use a bounded 60-second provider timeout;
  timeouts are distinct from provider input rejection. Daily payloads include
  expiryCode 0, matching the official example; this is not proof of daily API
  availability, which must be checked live after deployment.
- Saved history loads independently of status and chains. Loading/errors no longer
  masquerade as an empty archive. Exports keep their storage kind; CE/PE, expiry
  selection and date range are visible. Check quality preserves raw records and
  reports missing, outside-range and off-session rows.
- /api/dhan/coverage combines at most 500 saved history records for a requested
  range of at most 31 days. It separates index, side and relative-expiry variants,
  deduplicates identical timestamp/strike observations, excludes conflicting
  observations and reports them. Normalized exports include only actual candles
  inside the requested date range and 09:15–15:29 IST. Raw records are untouched.
  Weekday grids do not establish exchange holidays or special-session coverage;
  rolling ATM data is not a fixed-expiry, fixed-strike trade series.
- deploy/check-data.py checks the live version, recent saved coverage and the
  three index daily requests. It reports provider rejection as PENDING rather
  than declaring completion. It does not alter authentication or cloud settings.


## Official DhanHQ-py setup (3.2.0)

`dhanhq==2.3.0` is pinned with its transitive dependencies. The provider uses
public SDK methods for daily/minute history, rolling expired options, expiry lists,
option chains and LTP/OHLC/full quote snapshots. The adapter unwraps only the SDK
envelope, preserving the original Dhan response and existing raw record format.
Every request creates a fresh SDK context from the existing rotating credentials.
SDK requests run outside the ASGI event loop, retain bounded timeouts, reject
redirects, sanitize transport errors before SDK logging, and expose only market-data
endpoints. Instrument master downloads retain the existing original-CSV/checksum path.

Live, 20-depth and 200-depth capture now uses official SDK subscriptions and
packet decoders. The 200-depth socket uses the SDK's root endpoint. Original
binary frames remain stored even if a particular packet cannot be decoded.
These are still bounded ten-second samples, not continuous historical order books.
Foreign stock feeds require separate credentials/entitlements and are not enabled
by installing this SDK. No trading/order/account mutation methods are exposed.

### One-time deployment from the existing authenticated Cloud Shell

Run `bash ~/theta-update-311/live-app/deploy/setup-sdk.sh` after fetching/checking
out this release. It deploys the existing app with the usual verified rollout,
then creates/updates the private `theta-plus-collector` Cloud Run Job from the
serving image digest and existing service account, bucket and Dhan secret references.
Only data configuration is copied to the worker, not the Admin password hash. It
grants that existing service identity `roles/run.invoker` on that job only and
adds `THETA_COLLECTION_WORKER` to the service. Existing service settings and data
are retained. Worker CPU/memory is allocated only while executions run.

After deployment, `/api/collection/engine` reports SDK version and worker state.
An Admin-authorized Resume dispatches a Cloud Run execution with service identity;
no Admin password is persisted or sent to the worker. The UI polls progress while
open; closing it does not stop an already dispatched worker. Pause, quota, token,
permission and rejected-request states remain durable stop conditions.

The worker handles existing `running` plans under shared CAS leases, skips paused
plans, paginates job discovery, and never creates or expands a user's plan. A
bounded execution checkpoints and dispatches its successor for a long backfill.
A stopped token-required plan needs an explicit Resume after updating the token.
If the worker is not configured, the existing browser/terminal runner still works
and is labelled accordingly. SDK/worker configuration is not proof of full historical
coverage; inspect the collection ledger and coverage checks separately.

## Whole-master instrument access

**All instruments** searches every retained non-synthetic catalogue page, including
unmapped identities with their recorded review reason. Filters use exact documented
API segments; placeholder expiry dates do not remove cash equities or indices.
Selection carries both the immutable catalogue SHA and entry key. A master change
requires a new selection. Quotes also require a master no more than 24 hours old.

The selected instrument can read its retained recorder observation without opening
another socket, or use the Admin-protected quote/history actions. These actions use
the existing Dhan connection, shared request lease and daily API budget. They never
accept a client-supplied segment/security-ID override. Quotes preserve the raw
response and timestamp limitations. History preserves its source before validation,
then uses the existing exact-series candle store and requested-session grid checks.
Rejected arrays do not become completed histories. Intraday requests accept the
provider's 1/5/15/25/60-minute intervals and at most 31 completed dates; daily requests
accept at most 366 completed dates. `To` is exclusive. Provider retention,
entitlements and invalid/no-data responses remain explicit.

Continuous recording has four 5,000-instrument marketfeed shards, leaving one of
the documented five connections for the separately leased interactive research
screen. Watch instruments and current NIFTY/BANKNIFTY options retain priority;
remaining current, documented catalogue identities fill recorder slots in
indices/futures, equities, then options order. Exact `(segment, security ID)` pairs
are deduplicated. No `NSE_M` mapping is invented. Additional current identities use
the existing all-catalogue quote/history sweeps and the selected-instrument actions.
The status screen reports eligible count, capacity and overflow separately.
These are subscription counts, not proof every subscribed instrument traded or
sent frames. Depth limits remain 50 instruments for depth20 and one for depth200.
Recorder receipts and last-trade times never substitute for exchange quote time.
The release still reports `all_market_continuous: false`.

The GitHub main-branch release workflow checks Python/JavaScript, verifies serving
assets including the instrument screen, then activates and verifies the matching
collector build. Its existing rollback guard remains in effect.
# Daily rejection recovery and quota rollover

Long daily-history requests rejected with DH-905 or DH-907 now receive one
deterministic date-window recovery job. A recent 30-day probe must return valid
source rows before earlier windows are requested. The windows cover the exact
original range, with at most 128 requests per instrument and eight active window
jobs. Empty or rejected probes remain unresolved; they do not establish that
older history is unavailable. Original results and per-window outcomes remain
retained. Retry children never become automatic retry sources.

The shared daily API guard preserves the current task and waits until 00:00:05
IST the following day. The worker then resumes automatically. Explicit manual
pauses remain paused. Legacy quota pauses resume only after their recorded IST
day ends, with a valid timezone-aware timestamp and no manual-pause marker.
