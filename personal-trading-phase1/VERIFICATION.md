# Phase 1 — Verification & Testing Guide

This guide matches the delivered `personal-trading-phase1` bundle. It does not
assume access to your machine. Run commands yourself and report their outputs.
Docker integration, live Dhan access and database writes are not yet certified.

Use Linux/macOS Terminal, Windows PowerShell, or WSL. All `docker compose` commands
below are single-line and work in those shells. Start Docker Desktop on Mac/Windows
or Docker Engine on Linux. Use `python3` on Linux/Mac and `py -3` on Windows for the
host setup helper. Inside containers, always use `python`.

All commands run from the repository's `personal-trading-phase1` directory.
The verification helper is included as `app.verify_phase1` in current images and
needs no host Python packages.
It calls only the existing API, reads the mounted application API key internally,
and prints no token/key. It does not place trades, delete data or change MCX mapping.

## 1. Startup and health

```text
cd personal-trading-phase1
docker version
docker compose version
```

If you have not created `.env` and the local secret files, run just one:

```bash
# Linux/macOS/WSL
python3 setup_secrets.py
```

```powershell
# Windows PowerShell
py -3 setup_secrets.py
```

Enter the Dhan Client ID and current token locally. Leave existing working
configuration alone if already set up. Then:

```text
docker compose config --quiet
docker compose up --build -d
docker compose ps --all
```

Expected after startup settles:

| Service | Expected |
| --- | --- |
| `postgres` | Running, healthy |
| `redis` | Running, healthy |
| `api` | Running, healthy |
| `worker` | Running; verify responsiveness with Celery ping below |
| `dispatcher` | Running; verify publication during the historical task test |
| `migrate` | Exited with code 0 — this one-time service should finish |

Worker/dispatcher do not define Docker healthchecks. A blank health field is not
a failure, and "running" alone does not prove their application logic is working.
Normal `docker compose ps` omits stopped services, so use `--all` for migrations.

```text
docker compose logs --tail=50 migrate
docker compose exec -T postgres pg_isready -U trading -d trading
docker compose exec -T redis redis-cli ping
docker compose exec -T api python -c "import httpx; r=httpx.get('http://127.0.0.1:8000/health/ready',timeout=10); print(r.status_code,r.json()); r.raise_for_status()"
docker compose exec -T worker celery -A app.celery_app:celery_app inspect ping --timeout=10
docker compose exec -T worker celery -A app.celery_app:celery_app inspect registered --timeout=10
docker compose exec -T worker celery -A app.celery_app:celery_app inspect active_queues --timeout=10
```

Expected signals:

- Migrate: `Schema v1 and historical_data hypertable ready`.
- PostgreSQL: `accepting connections`.
- Redis: `PONG`.
- API: HTTP 200 with `database: true`, `redis: true`, `phase: 1`, `mcx: HOLD`.
- Celery: at least one `history@...` node returns `pong`.
- Registered tasks include `history.fetch`; active queues include `history`.

Optional host API check, testing the published port as well:

```bash
# Linux/macOS/WSL
curl --fail --show-error http://127.0.0.1:8000/health/ready
```

```powershell
# Windows PowerShell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/ready'
```

If `API_PORT` is not 8000, replace only the host command's port. The API listens on
8000 inside the container regardless of the host port setting. On Windows use
`curl.exe` when you specifically want curl syntax; `curl` can be an alias.

## 2. TimescaleDB, tables and constraints

The actual candle table is **`historical_data`**, not `ohlcv`. Expected public
tables are `instruments`, `historical_data`, `watchlist`, `fetch_jobs`, and
`request_attempts` (five tables).

```text
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT version();"
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'timescaledb';"
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT hypertable_schema, hypertable_name, num_dimensions, num_chunks FROM timescaledb_information.hypertables WHERE hypertable_name = 'historical_data';"
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name;"
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'historical_data' ORDER BY ordinal_position;"
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid = 'public.historical_data'::regclass;"
```

Pass criteria:

- Exactly one `timescaledb` extension row with a version.
- One hypertable row for `public.historical_data`; `num_chunks = 0` is normal
  before any candle data has been inserted.
- All five application tables exist.
- Candle timestamp type is `timestamp with time zone`; OHLC fields are numeric
  and volume is bigint.
- Candle primary key includes `exchange_segment`, `security_id`, `interval`,
  and `timestamp`; OHLC/volume checks and instrument/job foreign keys exist.

Optional detailed psql views (these also work in PowerShell):

```text
docker compose exec -T postgres psql -U trading -d trading -c "\d+ public.instruments"
docker compose exec -T postgres psql -U trading -d trading -c "\d+ public.historical_data"
docker compose exec -T postgres psql -U trading -d trading -c "\d+ public.watchlist"
```

## 3. Dhan authentication

Use the verification helper included in the current container image:

```text
docker compose exec -T api python -m app.verify_phase1 auth
```

Expected:

```json
{"authenticated": true, "http_status": 200, "provider_code": null}
```

Followed by `PASS: Dhan profile authentication`. This checks the `/auth/check`
endpoint with your application API key and makes Dhan's authenticated profile
request. It does not expose personal profile data. Profile authentication alone
does not prove that the separate historical Data API subscription is active.

If you want a simple direct Python check without the helper:

```text
docker compose exec -T api python -c "from app.dhan import check_authentication; print(check_authentication())"
```

This direct variant tests Dhan credentials but bypasses your API endpoint/API-key
check; the helper tests both. No raw access token is needed in a command line.

## 4. Real historical response and Celery worker test

This smoke test uses **one real instrument and one closed date**, not invented
dummy candles. It exercises API registration, durable PostgreSQL job creation,
dispatcher publication, Redis delivery, Celery processing, Dhan HTTP and candle
persistence together.

The example identity is NIFTY, security ID `13`, segment `IDX_I`, type `INDEX`.
Confirm it in the current official Dhan instrument master before passing
`--confirm-identity`. The helper does not import or auto-remap a catalogue. If the
identity already exists with different metadata, it stops for review.

Start with daily history for October 23, 2026. `end` October 24 is exclusive.
If testing later, use another known closed trading date within the provider horizon.

```text
docker compose exec -T api python -m app.verify_phase1 fetch --security-id 13 --symbol NIFTY --segment IDX_I --instrument-type INDEX --interval 1d --start 2026-10-23 --end 2026-10-24 --confirm-identity
```

Expected sequence:

1. `DURABLY_REGISTERED` plus a `job_ids` UUID.
2. `QUEUED`, possibly `RUNNING` or `RETRY_WAIT`; fast transitions may not all be
   visible in polling.
3. Final `ROWS_RECEIVED`, positive `rows_received`, zero `conflicting_rows`.
4. An attempt record with actual HTTP timestamps, HTTP 200 and `ROWS_RECEIVED`.
5. Helper prints `PASS: historical job ...` and exits with status 0.

To check minute candles as a separate task:

```text
docker compose exec -T api python -m app.verify_phase1 fetch --security-id 13 --symbol NIFTY --segment IDX_I --instrument-type INDEX --interval 1 --start 2026-10-23 --end 2026-10-24 --confirm-identity
```

Use the returned UUID below; replace `JOB_UUID` without angle brackets:

```text
docker compose exec -T api python -m app.verify_phase1 job JOB_UUID --timeout 300
docker compose exec -T api python -m app.verify_phase1 summary
```

The helper's polling timeout does not cancel the job. If it prints `PENDING`,
inspect the durable status and logs before dispatching again. A shared cooldown
or daily budget guard can delay the next due time.

Confirm actual saved candles rather than relying only on a task log:

```text
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT exchange_segment, security_id, interval, count(*) AS candles, min(timestamp) AS first_utc, max(timestamp) AS last_utc FROM historical_data WHERE exchange_segment = 'IDX_I' AND security_id = '13' AND timestamp >= TIMESTAMPTZ '2026-10-23 00:00:00+05:30' AND timestamp < TIMESTAMPTZ '2026-10-24 00:00:00+05:30' GROUP BY exchange_segment, security_id, interval;"
```

You should see the tested intervals with positive counts. Daily NIFTY on one
normal trading date is expected to return one candle. A regular NIFTY minute
session commonly has 375 candles; this smoke test does not independently certify
the exchange calendar or minute coverage. Investigate a lower count rather than
filling missing candles with fabricated prices.

Repeating the exact same test returns the same durable job, and `inserted_rows`
can be zero if identical candles came from a previously collected overlapping
range. That is not a duplicate-write failure. A terminal existing job is not
automatically fetched again. For a new execution, choose a different closed
date/range instead of deleting rows or resetting attempts.

## 5. Logs and exact success evidence

```text
docker compose logs --since=15m --tail=100 migrate api
docker compose logs --since=15m --tail=150 postgres redis
docker compose logs --since=15m --tail=200 dispatcher worker
docker compose logs --follow --tail=100 dispatcher worker
```

Use Ctrl+C to stop following logs; it does not stop containers.

| Log source | Useful evidence |
| --- | --- |
| `migrate` | Exact application message: `Schema v1 and historical_data hypertable ready` |
| `api` | Uvicorn startup normally includes `Application startup complete.`; actual readiness is the HTTP check |
| `postgres` | Server reports readiness to accept connections; SQL checks prove the extension/schema |
| `redis` | Server reports readiness; `PONG` proves responsiveness |
| `dispatcher` | Application message: `Queued 1 durable historical jobs` (number can differ) |
| `worker` | Task receipt for `history.fetch[...]`, then structured `"event": "dhan_attempt"` with your durable `job_id` |
| Worker attempt | `"outcome": "ROWS_RECEIVED"`, HTTP 200, positive `"rows"`, timing fields and expected segment/date range |
| Durable job + database | `ROWS_RECEIVED`, positive row count, no conflicts and actual corresponding candle rows |

The bundle does not log a custom literal `SUCCESS` message. Celery's generic
`Task history.fetch[...] succeeded` means the task handler returned; it can also
occur when a duplicate delivery was skipped or a job was flagged for review.
It is **not** sufficient evidence of successful data collection. Celery's
bracketed task ID is separate from the application job UUID in `dhan_attempt`.
The dispatcher can publish another delivery ID for the same durable job.

Actual HTTP evidence for a specific job:

```text
docker compose exec -T postgres psql -U trading -d trading -v ON_ERROR_STOP=1 -c "SELECT job_id, attempt, started_at, http_started_at, finished_at, duration_ms, gate_wait_ms, since_previous_gate_ms, http_status, provider_code, outcome, rate_limit_evidence, retry_delay_seconds, response_rows FROM request_attempts WHERE job_id = 'JOB_UUID' ORDER BY attempt;"
```

No `request_attempts` row is expected when blank credentials stop the job before
an HTTP request starts. A pre-request receipt left by an interrupted worker is
not a completed provider response.

## 6. Common failures and safe fixes

| Failure | Likely reason and next action |
| --- | --- |
| Docker daemon unavailable | Start Docker Desktop/Engine, then repeat `docker version`. On Linux resolve Docker socket access using your installation's normal permissions policy. |
| Compose file not found | Change into the directory containing `compose.yaml`; keep its `.env` and `secrets` directory there. |
| Missing secret file / permission denied | Run the setup helper as your local user; ensure Docker has file-sharing access to this directory on Mac/Windows. Do not print secret contents. |
| Port 8000 already allocated | Change `API_PORT=8001` in `.env`, run `docker compose up -d api`, and use 8001 only in host-side HTTP checks. |
| Image pull/build error | Check Docker registry access, available disk/RAM, platform compatibility and the configured image tags. Do not replace TimescaleDB with plain Postgres. |
| `migrate` exits nonzero | Read `docker compose logs --tail=100 migrate postgres`. The API/worker/dispatcher wait for successful migration. Fix the cause, then use `docker compose run --rm migrate` followed by `docker compose up -d`. |
| Timescale extension missing / function absent | Confirm the configured Timescale image and successful extension initialization. Use the extension and hypertable SQL checks. |
| PostgreSQL connection refused | Confirm `postgres` is healthy and in the same Compose project/network. Inside app containers, DB host is `postgres`, not `localhost`. |
| Database password authentication failed | A named volume may contain an older password. Changing a secret file does not change an existing PostgreSQL role password. Restore the matching secret or rotate the role through an authenticated administrative session. Do not delete the volume as a shortcut. |
| Redis connection refused | Check `redis` status/logs and network. App broker URL is `redis://redis:6379/0`, not host localhost. |
| Redis `OOM command not allowed` | `noeviction` intentionally prevents silently losing queued jobs. Review backlog, allocated memory and disk; increase Redis/VPS capacity as appropriate. Do not flush the DB/queue to hide the issue. |
| API HTTP 401 `INVALID_API_KEY` | Application key mismatch. The supplied helper reads the mounted key internally; this is separate from Dhan's token. |
| API 503 `API_KEY_NOT_CONFIGURED` | Missing/short application key file; rerun local setup and recreate affected containers if needed. |
| Dhan `AUTH_REQUIRED`, DH-901, 807–810, HTTP 401/403 | Invalid/expired token or authorization issue. Enter the current token via setup and rerun authentication. HTTP 403 may also require investigating provider access restrictions. |
| DH-902 / 806 / access review | Check Dhan Data API subscription/permissions. Profile authentication can pass even when historical access does not. |
| DH-907 | Ambiguous incorrect-parameters/no-data response. Inspect exact segment, instrument type, ID and closed date range in the attempt/job. Do not label it rate-limiting or delete the instrument. |
| HTTP 429, DH-904 or 805 | Explicit provider rate/connection-limit evidence. Inspect actual request timing, Retry-After/cooldown and other software using the same Dhan account. This app's limiter does not coordinate with Theta Plus. |
| Empty response after bounded retries | Job/instrument should remain `FAILED` for manual review; identity and stored data stay intact. Check dates/session/provider availability. |
| `NEEDS_MAPPING` | Exact instrument identity is not verified/supported. Repeated registration does not automatically overwrite an existing mapping. Review it; MCX remains HOLD. |
| Job stays `QUEUED` | Check dispatcher logs, Celery ping, registered task and `history` queue. Check `next_attempt_at`; worker replay may wait up to about five minutes after an earlier dispatch. |
| Job is `RETRY_WAIT` | Inspect `next_attempt_at`, retry delay, cooldown and daily guard. Let it reach its due time; a queue ping does not bypass pacing. |
| `INVALID_RESPONSE` / candle conflict | Inspect request/attempt metadata and the stored series. Do not silently overwrite conflicting candles or manufacture missing values. |
| API validation HTTP 422 | Check numeric security ID, allowed interval, exact segment/type and `start < exclusive end <= today IST`. Historical intraday data also has a five-year horizon. |

For token replacement, run the host setup helper again. Blank Client ID input
preserves an existing value. Resume only an `AUTH_REQUIRED` job with attempts left:

```text
docker compose exec -T api python -c "import sys,httpx; from pathlib import Path; r=httpx.post('http://127.0.0.1:8000/history/jobs/'+sys.argv[1]+'/retry',headers={'X-API-Key':Path('/run/secrets/api_key').read_text().strip()},timeout=30); print(r.status_code,r.json()); r.raise_for_status()" JOB_UUID
```

Other failed/exhausted jobs intentionally require manual review. Do not run
`docker compose down -v`, `redis-cli FLUSHALL`, or truncate tables for recovery.
The helper is part of the image; do not use `docker compose cp` with this read-only container.

## 7. Optional restart/replay acceptance

Perform this in the local test deployment when no important collection is active:

1. `docker compose stop worker`
2. Run the helper `fetch` command with a new small closed-date request and
   `--timeout 10`; expect `PENDING`, with the durable job still saved.
3. `docker compose start worker`
4. Run `verify_phase1.py job JOB_UUID --timeout 300` inside the API container.
5. Verify row persistence, attempt history and no conflicting/duplicate identity.

This checks graceful stop/start and queued-work delivery. It is not evidence of
crash/OOM recovery, broker-disk-loss recovery or live-market reconnect continuity.
There is no persistent live WebSocket recorder in this Phase 1 bundle.

## 8. Results to send back

```text
OS / shell:
Docker + Compose versions:
Container status: [paste docker compose ps --all]
Migrate exit code/message:
API ready result:
Redis PONG:
Celery pong + history.fetch registered + history queue:
Timescale version and hypertable row:
Five tables present:
Dhan auth result:
Historical job UUID / interval / date range:
Final status / attempts / rows / conflicts:
Relevant request-attempt row(s):
Relevant error logs, if any:
```

Send sanitized output only: omit tokens, API keys, raw secret files, full environment
dumps and personal profile responses. No screenshot or credential upload is needed.

Phase 1 smoke acceptance requires working services/schema/authentication, a job
actually processed by Celery, a recorded provider attempt and corresponding saved
candles. It does not mean full historical coverage, all-market ticks or production
readiness. Commodity mapping remains HOLD throughout.

Official references:
- https://docs.docker.com/reference/cli/docker/compose/ps/
- https://docs.celeryq.dev/en/stable/userguide/monitoring.html
- https://dhanhq.co/docs/v2/historical-data/
- https://dhanhq.co/docs/v2/annexure/
- https://dhanhq.co/docs/v2/instruments/
- https://github.com/dhan-oss/dhanhq-skills/blob/main/skills/dhanhq/SKILL.md
