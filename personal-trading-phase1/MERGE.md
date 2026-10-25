# One Theta Plus deployment

The new image includes the retained Theta Plus website and research APIs, the
FastAPI history API, Celery, Redis and TimescaleDB. The old GCS Store interface now
has a PostgreSQL implementation. The migration retains byte-exact source objects,
including old ticks, OI/chain responses, raw histories, diagnostics and paper records.
Understood daily/intraday candle series are additionally promoted into historical_data,
with exact identity checks, conflict retention and explicit eight-decimal normalization.
Rolling options remain separate source series. Unresolved mappings and MCX are not guessed.

Old and new prices/history reach the same instrument browser. New REST requests from
retained UI routes use the new shared account gate; old background job controls are
fenced in the merged application. The original GCS data are retained for rollback.

## Deployment and handover

One owner-run Cloud Shell command is still needed because the existing GitHub cloud
identity does not have Compute SSH / target discovery permissions. Do not paste Dhan
credentials into chat. After main CI integration succeeds, run from a clean checkout:

```bash
git clone https://github.com/signatureproprt-boop/Theta-plus.git theta-unified-merge
cd theta-unified-merge
python3 personal-trading-phase1/deploy/merge_cloudshell.py
```

For a tablet/browser that disconnects, use normal Cloud Shell (ephemeral mode off)
and run `bash personal-trading-phase1/deploy/start_merge_cloudshell.sh` from the
checkout. It performs authorization/SSH preflight interactively, then launches
with nohup, private logs at `~/theta-merge.log`, and a singleton workspace lock.
Browser disconnects do not terminate the detached process. A full Cloud Shell VM
shutdown still does; rerunning reuses generation-matching, SHA-verified downloads
from `~/.local/state/theta-unified-merge/archive`. Tokens stay in memory and are
refreshed using the owner's existing gcloud login.

Archive enumeration is paginated (200 objects), with twelve streaming downloads,
provider MD5/CRC32C checks and a durable SQLite checkpoint after each verified
object. The manifest and quiescent inventory digest stream without retaining the
entire bucket listing in memory. A single compressed tar/SSH stream transfers the
mirror to the VM. Source and stale local archive objects are never deleted; only
objects in the current verified manifest are imported. Local disk exhaustion stops
explicitly with checkpoints retained. Persistent Cloud Shell home storage remains
limited: an archive exceeding available space requires a larger migration runner;
do not delete market data to make room. No complete migration or coverage is claimed
until the final handover checks pass.

Each download worker reuses a verifying HTTPS connection pinned to the source GCS
bucket host, avoiding a fresh TLS connection for each small file. Media redirects
are rejected, partial/error responses discard their connections, and retries remain
bounded. The copy log includes UTC timestamps and observed files/second. These
changes affect archive transfer only; provider request gates remain in place.

To upgrade an already running copy from the same checkout, pull the verified main
revision and run `python3 personal-trading-phase1/deploy/upgrade_archive_runner.py`.
It matches only the exact checkout's Python process, pauses it, checks that there
are no active child commands and that the latest phase is archive-copy progress,
then verifies the public source still reports LEGACY_BACKEND. If any check fails it
resumes the existing process and defers the upgrade. It never interrupts database
import or collector ownership handover. An accepted upgrade terminates the paused
download and starts the new background runner with all verified checkpoints intact.

The script uses the existing VM and Cloud Shell identity. It does not create a second
VM, change IAM, expose public database/Redis/API ports or delete source data. It adds
a dedicated private Cloud Run subnet and restricts VM port 8000 to that source range. It first upgrades
the release manager to package both source trees, and still requires the named Docker
integration check to pass. It preserves the old admin password hash and reads/writes
one shared Dhan credential pair; only API credential file mounts are writable. Other
services continue using read-only mounts.

The handover performs:
1. Initial GCS mirror and checksum-verified PostgreSQL import while the old collector runs.
2. A generation-checked source fence, pause of matching collector Scheduler jobs, and
   graceful cancellation/terminal verification of old collector executions.
3. Final delta import, source inventory stability verification, and destination checksums.
4. Verification of old recording identities against the new master, then activation of
   the new live owner. Mismatched active identities stop handover for review.
5. Confirmation of every non-empty quote connection, same-URL gateway promotion and
   a durable NEW_BACKEND source marker.

Failures after fencing stop the new live process and restore the old marker, paused
Scheduler jobs and collector. Imported data remain for retry. A process kill or loss of
Cloud Shell during the critical section can leave FENCED; inspect and recover its owner
before rerunning. Source archive is never deleted. Archive verification does not certify
all instrument/date coverage or a complete conversion of every old data format.

The existing run.app URL remains the single user entry point. The owner script prepares
and probes a no-traffic Cloud Run gateway candidate with Direct VPC egress, while the
VM API keeps its loopback binding plus an internal-IP binding. A dedicated source subnet
and VM-specific allow/deny firewall rules restrict port 8000; there is no public API port.
Only after data import and live-owner verification does traffic move to that candidate.
HTTP and browser WebSocket requests reach the merged app. Health/fence routes stay local
on Cloud Run so rollback and deployments can still verify the source fence. Failure
restores original traffic and the original network-free service template. The original
GCS archive remains available for recovery, with no automatic writes by the old collector.
Source documentation: https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc


## Phase 2 in the unified runtime

- A dedicated Compose `live` service owns a PostgreSQL advisory lock, avoiding one
  feed per Uvicorn worker. It starts only after the verified archive/legacy-stop marker.
- Up to four quote sockets, each capped at 5,000 identities; fifth reserved for research.
  Watchlist snapshots refresh every 30 seconds. Expired/blocked identities are excluded.
- The old 20/200-depth adapters are retained with supported, verified non-MCX scopes;
  depth has separate provider limits and remains explicitly uncertified for lossless capture.
- Tick events immediately enter a Redis Stream and Pub/Sub. No unpersisted entries are
  trimmed. A writer commits at 100 events or five seconds, acknowledges only after DB
  commit, and replays pending messages after restart. PKs deduplicate replayed events.
- Session-start receipts are durable. A separate planner loads DB checkpoints and creates
  bounded reconciliation jobs; it does not hold the socket loop while downloading history.
- Celery gap jobs reuse Phase 1 request schemas, response classifier, shared Redis account
  gate and immutable OHLCV insert policy. Current/settling minute is excluded. Retries
  have durable timing evidence and DH-907 stays separate from explicit rate-limit evidence.
- Empty/no-row responses finish in review after bounded retries. No instruments are deleted
  or invalidated. A nonempty response is never labelled complete session coverage.
- Catch-up scans every five minutes cover the reconnect settling tail. Immediate recovery
  is bounded to seven days; older archive gaps remain with the Phase 1 planner. Lost ticks
  and depth cannot be reconstructed from REST minute candles.

## Diagnostics

Archive download authentication recovery: `GOOGLE_AUTH_WAIT` means the owner Cloud
Shell login could not supply an access token. The downloader retries every 30 seconds
for up to ten minutes, retaining the current scan and verified checkpoints. Authorize
Google Cloud in another terminal using `gcloud auth print-access-token >/dev/null`.
`GOOGLE_AUTH_RESUMED` confirms recovery in the same process. Exhaustion stops with
`GOOGLE_AUTH_WAIT_EXHAUSTED`; this is not a Dhan credential failure. No token output
or subprocess error output is printed by this recovery path. A full host shutdown
still requires a resumed run; authentication recovery does not keep the host alive.

Resumed files still undergo generation, size and complete SHA verification. Their
existing durable checkpoints are retained; scan-marker updates commit once per
bounded listing page. New or repaired downloads continue to commit individually.

Inside the VM API container:

```bash
python -m app.verify_phase1 live-status
python -m app.verify_phase1 market-status
```

Authenticated routes: `/live/status`, `/live/watchlist`, `/live/latest`, `/live/gaps`.
`/live/ws` accepts API-key authentication in its first message, not a query URL. It is
live UI fanout, not replay. HTTP routes use `X-API-Key` from the protected local file.
The retained website uses its existing admin password for privileged data operations.

A queued stream or old receipt time is not evidence of fresh exchange data. Inspect
trade timestamps, pending-entry count, actual gap outcomes and the next market session.
Load/capacity and market-open continuity still require live acceptance testing.
# Persistent archive copy on the existing VM

Cloud Shell `nohup` survives terminal disconnection, **not a Cloud Shell host
restart**. For the long initial copy, use the existing persistent VM:

```bash
cd "$HOME/theta-merge-runner" && git pull --ff-only && python3 personal-trading-phase1/deploy/install_archive_vm.py
```

The installer requires successful Docker integration for the exact revision. It
refuses FENCED/NEW_BACKEND/public non-legacy state, an active VM live owner, or an
unrelated attached identity. It creates `theta-archive-reader` with
`roles/storage.objectViewer` **on the archive bucket only**, no project role and
no key file. Attaching the identity to an identity-free VM requires **one VM
stop/start**; the persistent disks remain. This does temporarily interrupt the
VM services. The public legacy service continues to serve its existing URL.
This is an explicit exception to the original identity-free VM design; the
ordinary handover script still makes no IAM changes.

Cached Cloud Shell files and a consistent SQLite backup are seeded to
`/srv/personal-trading/archive-worker` over one compressed IAP stream. Nothing is
deleted from Cloud Shell or GCS. A failed seed leaves the original cache intact;
rerunning the installer can retry it. The worker revalidates generation, size
and SHA before reuse. GCS downloads retain the provider checksum validation.

The enabled `theta-archive-copy.service` owns the copy, retries failures with a
60-second delay, and resumes from the data disk after a VM reboot. Metadata
credentials renew automatically. No gcloud/user session is used by the worker.
The UID 10001 service can write only its protected workspace. No broker requests,
database import, collector fence or traffic promotion are made by the installer.

Progress, including real object counts, is in the VM journal:

```bash
gcloud compute ssh personal-trading-phase1 --project=theta-pluse --zone=asia-south1-a --tunnel-through-iap --command='sudo journalctl -u theta-archive-copy.service -n 10 --no-pager'
```

After `VM_ARCHIVE_COPY_COMPLETE`, the existing `start_merge_cloudshell.sh`
automatically consumes this VM archive instead of downloading again in Cloud
Shell. Its final fenced pass requests a fresh VM scan, waits for a request-bound
manifest receipt, imports into the database, and keeps the original checksum,
source-stability and live-handover gates. **Copy complete does not mean merge
complete.** The owner handover still runs in Cloud Shell; a host loss after the
FENCED stage needs fence recovery review rather than a blind restart.
