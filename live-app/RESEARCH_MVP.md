# Theta Plus research workspace 3.3.0

Option buying only. Read-only decision support. No order placement or legacy
analysis endpoints are enabled. Research rules are hypotheses, not calibrated
confidence scores or validated trading recommendations.

## Implemented phases and boundaries

0. Data Quality: timestamp contracts, bounded retained-record quality, overlap
   conflict rejection, regular-session coverage check, token last-check and job
   heartbeat visibility. Reading this workspace never changes collection state.
1. Trading Room: real saved NIFTY/BANKNIFTY index candles, complete 1/5/15/60m
   session-anchored bars, EMA20/50, historical session levels, contract labels,
   freshness and a compact market sidebar. No invented chart data or index VWAP.
2. Options: current-expiry bounded manual fetch through the existing Dhan route;
   chain, strike selection, spread percentage, activity/identity flags, OI change,
   full-returned-chain OI and volume PCR. Missing/expired quote times hide IV and
   Greeks. No PCR history or IV percentile without verified observation history.
3. Two coded setups and a cost-aware position planner. Admin-protected immutable
   observation journal with rule version and deviations. Observation notes are
   NOT simulated fills, do not produce trade P&L and do not satisfy the 50-trade
   forward-test requirement. Automated paper execution remains blocked pending
   a verified timestamped quote/context source and exact-contract data.
4. Bounded event-clock underlying replay. No candles closing after the event
   clock reach the rules. Option P&L, performance statistics and walk-forward
   results are explicitly gated; a complete option-trade backtester is not
   claimed. Exact contract history, historical execution quotes/costs and calendar
   validation are outstanding data dependencies.

Global market feed integration, expiry comparison, continuous streaming and live
execution remain outside this release. Existing collection is independent and
keeps its checkpoint/worker configuration; the worker image need not be replaced.

## Timestamp contract

All timestamps require offsets, normalize to UTC, and display with IST labels.
Keep exchange time and received time separately. Server receipt never substitutes
for exchange quote time. A timestamp in the future, missing ingest time or age
above the dataset policy blocks live eligibility. Last-trade time is not a quote
timestamp. Historical data displays as historical even when recently fetched.

| Dataset | Age ceiling | Required provenance / fields |
| --- | --- | --- |
| Closed 1m candle | 90s from close | Exchange start/close, received time, OHLCV |
| Chain | 10s | Exchange quote time, received time, expiry, strike, side, security ID, bid/ask, volume/OI |
| Greeks | 10s | Quote time and provider source; stale values hidden |
| Futures VWAP | 90s | Exact futures symbol/expiry and timestamped volume; unavailable until integrated |
| Context | 60s | Source, observation time and session; unavailable until integrated |

Source: https://dhanhq.co/docs/v2/option-chain/ . Documented chain fields include
bid/ask, Greeks, IV, OI, previous-day OI and volume, but no exchange quote timestamp.
Consequently the current chain route's receipt alone cannot produce FRESH.
Historical source: https://dhanhq.co/docs/v2/historical-data/ .

PCR is sum(PE OI)/sum(CE OI), or separately sum(PE volume)/sum(CE volume), over all
returned strikes of the stated expiry. Missing sides/invalid required fields or
zero CE denominator yield unavailable. Values from a stale snapshot are explicitly
snapshot context, not a current directional forecast. Delta OI is versus the
provider's previous-day OI, not a manufactured intraday change.

## Rules: research-1.0

Use only complete closed bars. Anchor intraday buckets to 09:15 IST. Discard
incomplete/gapped buckets. 1H/15m EMA20 and EMA50 need their real warm-up; do not
fill missing candles. The weekday session filter is not exchange-calendar proof.

Common eligibility: aligned 1H/15m bias, 09:30 <= clock < 15:00, current event
context, direction-aligned breadth >= 60%, room to the next obstacle >= 1.5R,
verified contract and current lot metadata, quote age <= 10s, spread <= 1% of
bid/ask midpoint, OI >= 100, volume >= 100. Expiry-day entries default blocked.
These thresholds are explicit unvalidated defaults and must not be optimized
against the same period later reported as out-of-sample performance.

Opening range: require every minute of 09:15–09:30. Break is a closed 5m candle
beyond the edge by 0.05%. Retest within +/-0.05% of that edge must hold on close;
a later 5m close crosses the retest candle's high (CE) or low (PE). Maximum six
5m bars after first break; otherwise LATE_ENTRY. Retest extremum is invalidation.

Trend pullback: aligned HTF bias; closed 15m bar intersects prior EMA20 within
0.05% and holds on the directional side; latest 5m close crosses previous 5m
high/low. Pullback extreme is structure invalidation.

State precedence: DATA_UNAVAILABLE for required missing/invalid evidence;
NO_TRADE for a failed eligibility check; WAIT for pending price-event conditions;
CONFIRM only if all gates pass. CONFIRM is still unvalidated research, never an
order. Reason codes are returned with passed/failed/pending/missing lists and
the exact rule version/config/clock. Current source limitations prevent live
CONFIRM by design.

## Risk contract

Quantity = lots * verified lot size. Risk per lot includes (entry - stop) * lot,
explicit exchange/SEBI charges, applicable GST, sell-side STT, buy-side stamp,
spread and slippage allowances. Round-trip brokerage and its GST are fixed.
Use floor((available risk - fixed costs)/per-lot risk), capped by affordable
premium plus estimated costs. Available risk is the minimum of per-trade budget
and daily loss cap less realised losses and all reserved open risk. No hidden
tariff defaults. Source/effective date are mandatory manual assumptions, not an
automatically verified current tariff feed. Old lot metadata, future-effective
cost schedule, expired contract or disallowed expiry day blocks sizing.
Stop fills can gap; premium paid can exceed planned stop risk.

## API and persistence

- GET /api/research/workspace?symbol=nifty|banknifty : bounded evidence and rules.
- POST /api/research/risk : pure manual planning calculation, no persistence.
- GET /api/research/journal?after=... : canonical observation docs, paginated.
- POST /api/research/journal : existing Admin password required; fixed 32-hex
  client ID makes an identical retry idempotent; changed payload is a conflict.
- POST /api/research/replay : <=7 days, <=600 five-minute events, explicit aware
  event clock inside range, bounded retained raw-history scan.

Raw collector response archives are not silently declared covered by the
bounded legacy dhan_history view. Data Quality states the read scope. Full
universe collection completeness remains a separate job/outcome audit.

## Release

Run Python tests, both Node UI suites, JavaScript syntax and release shell syntax
checks. Use deploy/release-existing.sh for source update; it preserves service
environment and exact candidate rollout/rollback checks. No worker recreation,
automatic collection restart, IAM changes or token replacement is needed.
Authenticated cloud deployment and live smoke verification require the user's
configured Cloud Shell if unavailable in the Codex environment.


## Live LTP bridge (3.3.1)

Trading Room and research pages expose Start live / Stop live. Unlock the existing
Admin once in this tab before starting. The server holds the Dhan token; the
browser receives no token or authenticated provider URL. The Admin header is
sent only to the same-origin protected stream route. Lock Admin stops the live
stream in that tab. There are no order methods.

The bridge resolves the current master, subscribes NIFTY/BANKNIFTY/VIX/current
futures plus the selected underlying current-expiry ATM +/-2 returned option
strikes when master identity matches. It uses the official MarketFeed codec and
subscription builder. The original packet epoch retains the full LTT date; SDK
formatted HH:MM:SS is not used as proof of freshness. LTT <=10s is a recent trade,
not an exchange quote timestamp. Quote eligibility and Greeks remain blocked.

One separate cross-instance research/live-lease limits research sockets without
changing collection leases or job manifests. The SSE request rotates within
240 seconds including startup; the browser renews while the page stays connected.
Retry backoff is bounded; provider/auth failures require explicit restart. Heartbeat
stalls are labelled and trigger bounded reconnect. Ticks are coalesced once per
second for display, not saved as full-session OHLC. Observed LTP chart retains
at most 300 points, breaks across >10s gaps, and does not backfill gaps or claim
complete candles. Closing the page stops this research stream; the historical
worker remains independent. No unattended live recorder is claimed.

References: https://dhanhq.co/docs/v2/live-market-feed/ and the installed official
DhanHQ-py 2.3.0 MarketFeed source. Dhan supports up to five sockets per account;
the research bridge uses one. Other applications may consume the remaining quota.


### Live correction 3.3.2

Actual 3.3.1 verification received option ticks but no index ticks, and live LTT appeared 5h30 ahead of server UTC. Indices now use official SDK Ticker mode (15); derivatives retain Full mode (21). Dhan support confirms indices do not supply Full depth/OI: https://madefortrade.in/t/59525 .

The live adapter alone normalizes Dhan's IST wall-clock LTT epoch by subtracting 19800 seconds; raw_ltt_epoch and the convention remain in every observation for audit. Dhan support confirmation: https://madefortrade.in/t/57896/2 . This supersedes the initial assumption that this live field was a UTC epoch. Historical candle timestamps are unchanged. Tests cover mixed subscription modes, stale/future timestamps and UTC date rollover. Exchange quote time remains unavailable, so live depth still cannot unlock setup signals. Production verification of the corrected index stream remains a deployment check.


### Current-session chart 3.3.3

Starting live now also requests the selected underlying’s current-session one-minute OHLC from the official Dhan SDK. A separate Admin-protected endpoint refreshes at most once per 60 seconds from the browser; a shared 55-second cache bounds repeat calls. It does not take the live socket lease or change historical collection jobs. Provider request bounds are 09:15 IST through the current closed minute, capped at 15:30. Weekend/pre-open refreshes make no market data request. Source: https://dhanhq.co/docs/v2/historical-data/ .

The latest raw response and request provenance replace one durable research/session/<symbol> document. OHLC arrays must validate before replacement; duplicate conflicts, future/current incomplete bars, and off-session rows are excluded. Expected weekday-grid minutes and explicit gaps are returned, without claiming exchange holiday certification. Only current-day legacy chart rows are replaced; previous-day chart context remains. UI draws only complete timeframe buckets, preserves intraday time gaps, labels saved indicators separately and overlays recent observed live LTP without synthesizing candle highs/lows. Refresh failures retain evidence with an explicit error/stale label.

This chart path does not change setup confirmation gates, certify historical options P&L, or implement paper fills. Current-session candles and live LTP must still be verified against production after deployment. Existing option snapshot PCR/strike selection, cost-aware risk planning and immutable observations remain available; continuous PCR/IV history and 50 actual paper trades remain unestablished.
