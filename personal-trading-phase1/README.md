# Personal Trading — Phase 1

An independent FastAPI + Celery + Redis + PostgreSQL/TimescaleDB starter.
It does not modify or deploy the existing Theta Plus application. No orders,
instrument deletions, automatic remapping or commodity automation are included.

## Included

| File | Responsibility |
| --- | --- |
| `compose.yaml` | API, TimescaleDB, Redis, Celery worker, durable dispatcher and one-time schema initializer |
| `app/models.py` | Instruments, OHLCV hypertable, watchlist, fetch jobs and request-attempt ledger |
| `app/dhan.py` | Fresh token-file authentication, profile check, Dhan request formats, response classification and candle validation |
| `app/tasks.py` | Bounded retries, PostgreSQL duplicate-delivery fencing, non-destructive persistence and structured timing logs |
| `app/rate_gate.py` | Shared Redis account limiter, rolling 24-hour guard and account cooldown |
| `app/dispatcher.py` | Republish due PostgreSQL jobs after queue or worker loss |
| `tests/test_phase1.py` | Offline HTTP, retry, schema, identity, API protection and atomic Redis tests |

## Start locally or on your VPS

Prerequisites: Docker Engine, Docker Compose v2 and Python 3.12+ for the local
setup helper. Run these commands from the extracted project directory:

```bash
python3 setup_secrets.py
docker compose config --quiet
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
```

The helper asks for **your Dhan Client ID and current Access Token** with hidden
token entry. You can leave both blank to start infrastructure without contacting
Dhan. It generates a separate random API key and database password. Credentials
are local secret files, excluded from Docker builds and Git; they are not stored
in task arguments or request logs. Compose file secrets are read-only bind mounts,
not an encrypted vault. Their parent host directory is owner-only. For deployment,
keep that directory and your host account protected.

Only the API is published, on `127.0.0.1:8000`. PostgreSQL and Redis have no
published host ports. Use an authenticated TLS reverse proxy or SSH tunnel for
remote access. Do not change the API bind address to expose it directly.

The ready endpoint checks database/Redis availability, not Dhan subscription,
worker freshness, data completeness or market continuity. All other endpoints
require `X-API-Key`. Read the generated local API key into your shell without
printing it:

```bash
API_KEY="$(cat secrets/api_key.txt)"
curl --fail -X POST http://127.0.0.1:8000/auth/check \
  -H "X-API-Key: $API_KEY"
```

## Register an instrument and request history

Phase 1 has **manual registration only**. Verify the exact security ID, symbol,
API exchange segment and instrument type against the current Dhan instrument
master before setting `mapping_verified` to true. IDs alone are not globally
unique. Do not register placeholder values from the example below.

```bash
curl --fail -X POST http://127.0.0.1:8000/instruments \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"security_id":"YOUR_VERIFIED_ID","symbol":"YOUR_VERIFIED_SYMBOL","exchange_segment":"NSE_EQ","instrument_type":"EQUITY","mapping_verified":true}'

curl --fail -X POST http://127.0.0.1:8000/history/jobs \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"security_id":"YOUR_VERIFIED_ID","exchange_segment":"NSE_EQ","interval":"1","start":"2026-10-21","end":"2026-10-24"}'
```

`end` is exclusive. The example covers October 21–23, not October 24. Replace
dates as needed. Requests cover completed IST dates; intraday history is bounded
to the provider's five-year horizon. Daily history is not given an invented
five-year availability cap. Both families are split into at most 90-day windows
for bounded task sizes. Intervals are `1d`, `1`, `5`, `15`, `25`, `60`.

Every chunk has its own durable job. Repeating the same identity/date/interval
request returns the same job; it does not silently reset exhausted attempts.
The dispatcher publishes due jobs. API acceptance means registered in PostgreSQL,
not data already fetched. A maximum of four provider attempts per chunk is the
default; infrastructure retries do not make an instrument invalid.

```bash
curl --fail http://127.0.0.1:8000/history/jobs/JOB_UUID -H "X-API-Key: $API_KEY"
curl --fail 'http://127.0.0.1:8000/history/attempts?limit=100' -H "X-API-Key: $API_KEY"
curl --fail http://127.0.0.1:8000/history/error-summary -H "X-API-Key: $API_KEY"
docker compose logs --tail=100 worker dispatcher
```

Attempt pagination uses increasing `after` IDs. Filter by `jid=JOB_UUID` for one
job. The error summary groups attempts by segment, interval, HTTP status, provider
code, outcome and explicit rate evidence. It is not a unique-instrument coverage
or success percentage.

## Exact failure policy

| Observation | Action |
| --- | --- |
| HTTP 429, DH-904 or 805 | Record explicit rate/connection-limit evidence, respect Retry-After, back off and apply a shared account cooldown |
| DH-907 | Record ambiguous parameter/no-data rejection; bounded exponential backoff with jitter; no rate-limit inference |
| Empty candle arrays or blank body | Retry with bounded backoff; retain instrument, requests and dates; after exhaustion flag job/instrument `FAILED` for review |
| Authentication failure | Stop as `AUTH_REQUIRED`; replace token locally, then explicitly resume the job |
| Permission/subscription rejection | `FAILED` review; do not repeatedly send an unchanged request |
| Network error or provider 5xx | Bounded exponential backoff; distinguish from provider rate-limit evidence |
| Invalid OHLCV, malformed arrays or out-of-range timestamps | Reject response, preserve existing rows and flag review |
| Previously stored candle differs | Keep original candle, count conflicts and flag review; do not silently overwrite |
| MCX or commodity instrument type | Reject at registration and enforce `HOLD` again inside the worker |

An empty response never changes `mapping_verified`, deletes an instrument,
declares it delisted or proves its history unavailable. `NEEDS_MAPPING` is reserved
for an unverified/unsupported identity. `FAILED` is a collection-review flag.
Successful later windows do not automatically clear unresolved review flags.
No mapping probe, import worker or auto-fix exists for commodities.

Retry delay grows exponentially with equal jitter, capped at 300 seconds before
applying a larger Retry-After. Retries are driven by Celery, with the next due time
also persisted in PostgreSQL. Dispatcher replay is idempotent and cannot skip that
due time. All task arguments contain only the durable job UUID.

## Request evidence and rate limits

The attempt ledger records a durable pre-request receipt, actual HTTP start,
finish, duration, gate wait, previous gate spacing, recent gate admissions, HTTP
status, sanitized provider code, retry delay and SHA-256 of the response. Raw
authorization headers, tokens, response error strings and profile bodies are not
logged. An interrupted attempt remains `INTERRUPTED_UNKNOWN`, not a fabricated
failure or success. Compare actual `http_started_at` timestamps when investigating
bursts; gate timestamps alone are not proof of a provider violation.

Dhan documents Data APIs at 5 requests/second and 100,000/day. This starter defaults
to conservative 1,100 ms spacing and a 90,000-attempt rolling 24-hour guard. The
minimum configurable spacing is 250 ms (four gate admissions per second). Redis
Lua serializes admissions across all workers in **this installation**; Redis
failure blocks calls. Account limit responses set a shared cooldown. Redis uses
AOF and `noeviction`; PostgreSQL retains job state for replay after broker loss.

**Do not run this new collector and the existing Theta Plus collector against
the same Dhan account independently at full speed.** They do not share a limiter.
Consolidate dispatch or intentionally allocate a shared account budget first.
Other apps using that account also consume its provider allowance. Redis AOF
`everysec` can lose recent gate reservations on abrupt host failure; PostgreSQL
attempt timestamps are audit evidence, not a transactional shared limiter across
unrelated applications.

## Schema decisions

`Instrument` uses `(exchange_segment, security_id)` as its primary key. Candle
identity is `(exchange_segment, security_id, interval, timestamp)`. The timestamp
is `TIMESTAMPTZ`, represented in UTC; request dates and session interpretation use
Asia/Kolkata. Decimal prices avoid binary float storage. A Timescale hypertable's
unique key must include its time partition column. Watchlist references the same
composite instrument identity.

The initializer creates the extension, tables and seven-day hypertable chunks
under a migration advisory lock. It is a **version 1 initializer**, not an upgrade
framework: introduce explicit Alembic migrations for later schema changes.
It neither seeds sample instruments nor deletes data. The initial database owner
is also the runtime user; separate migration/runtime roles before final VPS
production sign-off. Named volumes persist across `docker compose down`.

## Token rotation

Rerun `python3 setup_secrets.py` on the host to enter the replacement token. The
helper writes in place so current Compose secret mounts see the changed content.
Client ID can remain unchanged; blank input keeps an existing value. Authentication
headers are read fresh for each request. The starter does not attempt unauthorized
login, token generation or renewal. Resume each blocked job without erasing its
attempt history:

```bash
curl --fail -X POST http://127.0.0.1:8000/history/jobs/JOB_UUID/retry \
  -H "X-API-Key: $API_KEY"
```

Only `AUTH_REQUIRED` jobs with attempts remaining can resume through this endpoint.
Other exhausted/error jobs remain for manual diagnosis in Phase 1. There is no
destructive reset endpoint. Replacing the API key/database password is a separate
operation; do not expect changing a password file to rotate an existing PostgreSQL
role automatically.

## Validation and remaining gates

Run the offline suite locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The delivered suite passed **41 tests**. It exercises provider-error distinction,
empty responses, response validation, token-file rotation, retry timing, atomic
parallel Redis admissions, cooldown/day guard, SQL compilation and API guards.
Redis tests use fakeredis with Lua, not a real Redis server. PostgreSQL statements
were compiled, not executed against TimescaleDB. Docker was unavailable in the
build workspace; container image pulls/builds, Timescale migrations, real broker
recovery and authenticated Dhan requests **have not been run here**.

On the VPS, check these before accepting Phase 1:

1. `docker compose config --quiet`, image build, successful migration and ready API.
2. Confirm `historical_data` is a Timescale hypertable using the SQL below.
3. Verify one current identity from the Dhan master and perform an authenticated
   daily and minute request; inspect attempts and returned timestamps.
4. Stop/restart the worker during a test job. Verify due job replay, preserved
   attempt history and no duplicate candle identities.
5. Restart Redis in an isolated test deployment and confirm due jobs resume from
   PostgreSQL; inspect pacing after a cold restore.
6. Resolve and pin audited image digests, split runtime DB privileges, configure
   VPS TLS/auth, backup/restore, disk alerts and worker/dispatcher freshness alerts.

```sql
SELECT hypertable_schema, hypertable_name
FROM timescaledb_information.hypertables
WHERE hypertable_name = 'historical_data';

-- Actual request-start spacing, independent of gate reservations:
SELECT job_id, provider_code, http_status, outcome, http_started_at,
       extract(epoch FROM http_started_at - lag(http_started_at)
         OVER (PARTITION BY account_key ORDER BY http_started_at)) * 1000 AS start_gap_ms
FROM request_attempts
WHERE http_started_at IS NOT NULL
ORDER BY http_started_at;
```

**This is Phase 1, not full production approval or historical coverage.**
`ROWS_RECEIVED` means validated rows were returned; `coverage_verified` remains
false. The starter does not yet reconcile exchange calendars, listing dates,
absent sessions or sparse-trading minutes. It does not invent missing candles.

Phase 2 must implement the live feed service, persisted subscription identities,
bounded reconnect with jitter, heartbeat/staleness detection, durable disconnect
gap windows, overlapping minute-history reconciliation, and frontend WebSocket
resubscription. Next.js, Tailwind and Lightweight Charts belong to that phase.
Do not run a persistent market socket inside a Celery task: use a dedicated feed
process and use Celery for bounded historical gap requests.

For Monday **26 October 2026** acceptance, check actual exchange timestamps and
receipt freshness, subscribed/observed identity counts, reconnect resubscription,
persisted gap boundaries and recovered minute ranges during the live session.
Minute backfill cannot reconstruct every missed live tick. The existing Theta
Plus restart evidence and scheduled market check do not certify this new system.
The Phase 1 bundle does not change that existing schedule.

## Official references checked 24 October 2026

- Dhan historical request schemas and horizons: https://dhanhq.co/docs/v2/historical-data/
- Dhan authentication/profile: https://dhanhq.co/docs/v2/authentication/
- Dhan error codes: https://dhanhq.co/docs/v2/annexure/
- Dhan endpoint rate limits: https://dhanhq.co/docs/v2/#rate-limit
- Celery task retry/idempotency: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery Redis visibility semantics: https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html
- Timescale hypertable initializer: https://www.tigerdata.com/docs/reference/timescaledb/hypertables/create_hypertable
- Docker readiness dependencies: https://docs.docker.com/compose/how-tos/startup-order/
