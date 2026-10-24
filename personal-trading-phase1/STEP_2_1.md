# Step 2.1 — Dhan v2 WebSocket client

Implementation: `app/live_feed.py`. Dependency: `websockets==15.0.1` (added to requirements).
This step is opt-in. No Compose service, FastAPI lifespan change, live deployment,
Redis publishing, tick storage, candle aggregation or REST reconciliation is enabled yet.

## What it does

- Opens one authenticated TLS connection. Secrets are read from the existing Phase 1 secret files.
- Selects verified, supported identities from the database Watchlist. MCX/commodity
  identities are excluded. More than 5,000 identities fails visibly; no truncation.
- Requests Quote mode (17), in JSON batches of 100; re-subscribes after reconnect.
- Decodes little-endian Quote (4), Ticker (2), OI (5), previous-close (6), market
  status (7) and disconnect (50) packets; supports concatenated whole packets.
  Unknown layouts, malformed lengths, unsupported segments and non-finite prices
  cause explicit errors. Index response code 1 and Full mode are not decoded in
  this Quote-only step; add verified layouts if they occur in provider testing.
- Emits trade epoch/UTC time separately from local UTC receipt time. Zero trade
  time stays null. Day OHLC and cumulative volume are never treated as minute OHLCV.
- Handles ping/pong through websockets. Its client ping timeout detects a dead
  connection even if the market is closed; a quiet instrument alone isn't a disconnect.
- Reconnects transient failures with equal jitter, exponential delays capped at 60
  seconds. Backoff resets only after a connection survives 60 seconds, not every handshake.
- Re-reads watchlist/token every 30 seconds; changes cause a fresh session.
- Provider 805 blocks further connection attempts to avoid fighting another collector;
  free a slot then restart the test process. 806–810 or HTTP 401/403 wait for changed
  credentials. HTTP 429 uses at least a 60-second delay. No DH-907 reclassification.
- Logs safe error classes, status/codes and delays, never raw exception messages,
  authenticated URLs, credentials or provider text. Transport logging is private.
- Cancellation closes the socket and awaits both child tasks.

## Test in the existing local Compose project

Only run when one Dhan account connection slot is free. This client does not coordinate
slots across the older Theta Plus collector, other apps or other hosts. Do not launch
multiple copies. Step 2.3 will address process ownership before background deployment.

From `personal-trading-phase1/`, with Phase 1 already running and configured:

```bash
docker compose build api
docker compose run --rm --no-deps api python -m app.live_feed --confirm-free-connection
```

These commands work in Linux/macOS terminals and Windows PowerShell with Docker Compose.
They apply to the **local** Compose project, not the VM release updater. Existing running
API/worker containers are not replaced by `build`; the temporary client uses the rebuilt
image and existing project database network/secrets. Press Ctrl+C to stop the test.
No need to send credentials in chat or put them in a shell command.

Watchlist must contain at least one verified identity. If it is empty, add an already
verified instrument through your existing database/admin workflow; this test never adds
or remaps instruments automatically.

Expected log sequence:

```text
LIVE_CONNECTED generation=1
LIVE_SUBSCRIPTIONS_SENT count=... generation=1
LIVE_SESSION generation=1 instruments=... reconciliation=NOT_INSTALLED
LIVE_FIRST_TICK generation=1
```

`LIVE_SUBSCRIPTIONS_SENT` means sent, not acknowledged. During a closed market you may
receive auxiliary packets or no new trades. First tick receipt alone doesn't prove price
freshness: inspect the event's `trade_time` against the exchange session and receipt time.

To exercise reconnect, briefly interrupt this test container's network access in your
local environment, restore it, and look for `LIVE_RETRY`, generation 2, subscriptions
sent again and another session event. Don't interrupt the production collector's network.

## Callbacks for later steps

```python
client = DhanLiveClient(on_event=enqueue_live_event, on_session=enqueue_reconciliation)
await client.run()
```

Both callbacks are async and must complete within two seconds. They should enqueue work;
slow/failed callbacks cause a visible retry rather than silently discarding events.
`on_session` runs on **every** subscription session, including the first process startup,
with generation, connection time, most recent disconnect-detection time and the full
instrument snapshot. This is a scheduling hook, not proof of a successful data response.
Step 2.2 must derive gaps from durable database checkpoints, not these process-local times.

The smoke-test callbacks only count packets. Nothing is durably saved or reconciled by
this step. REST minute backfills cannot recreate every missed exchange tick.

## Automated validation

From repository root, with dev requirements installed:

```bash
PYTHONPATH=personal-trading-phase1 python -m pytest -q personal-trading-phase1/tests
```

Tests include a real local WebSocket server: binary decoding, ping/pong, server close,
reconnect, resubscription, startup/reconnect events, token rotation, cancellation,
watchlist changes, commodity exclusion, 5,000 limit and credential-safe logs.
No real Dhan live session is claimed by these tests.

Protocol references:
- https://dhanhq.co/docs/v2/live-market-feed/
- https://dhanhq.co/docs/v2/annexure/
- https://websockets.readthedocs.io/en/15.0.1/reference/asyncio/client.html
