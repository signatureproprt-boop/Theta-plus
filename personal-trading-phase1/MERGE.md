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

The script uses the existing VM and Cloud Shell identity. It does not create a second
VM, change IAM, expose database/Redis/API ports or delete source data. It first upgrades
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
5. Confirmation of a new subscription session and a durable NEW_BACKEND source marker.

Failures after fencing stop the new live process and restore the old marker, paused
Scheduler jobs and collector. Imported data remain for retry. A process kill or loss of
Cloud Shell during the critical section can leave FENCED; inspect and recover its owner
before rerunning. Source archive is never deleted. Archive verification does not certify
all instrument/date coverage or a complete conversion of every old data format.

Public URL cutover is NOT automatic: the VM API remains loopback-only. The script prints
an IAP SSH tunnel command; use Cloud Shell Web Preview port 8080 to open the merged UI.
The previous run.app URL remains the preserved source until an HTTPS gateway/domain is
configured. No insecure public port is opened as a shortcut.

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
