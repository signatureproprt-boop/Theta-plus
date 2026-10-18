# Theta+ — runnable options research application

## Historical data audit screen (v11)

After unlocking the workspace, open **Data connections → Check backtest data**.
Choose a past trading date; the app uses the current Dhan server token to make
one NIFTY index minute-history request and two expired ATM-relative option
requests (CALL and PUT, MONTH expiryCode 1). The server stores an inventory of
field names, counts and availability, never raw historical values or credentials.
An unavailable endpoint remains unavailable in the report; no fabricated
prices fill gaps. A returned rolling ATM series does not provide historical
bid/ask or a fixed contract identity for option-entry/exit replay. The screen
always labels a fixed-strike quote backtest as NOT READY until that specific
data source is collected and verified separately. Historical index candle
availability alone does not establish option-buying expectancy.

## Daily Dhan token screen (v10)

Client ID remains fixed in the server's existing `theta-dhan-client-id` secret.
Run `bash deploy/enable-token-screen.sh` once in authenticated Google Cloud
Shell after deploying this version. The script grants the Cloud Run service
account only accessor and secret-version-adder permissions on the token secret
and sets `THETA_DHAN_TOKEN_SECRET`; it never asks for a token. In the app, unlock
your private workspace and open **Data connections → New Dhan access token**.
Paste the newly generated Dhan token into the password field and save it.
The API never returns it. The server reads Secret Manager `latest` at runtime,
with a 20-second cache, so multiple instances converge without a redeploy.
Existing pinned environment token is ignored when this screen is enabled.
If the Secret Manager read fails, requests fail closed rather than using an old
token. Refresh the research view during market hours to verify data entitlement.

Do not send the token in chat, screenshots, journal notes or imported JSON.
The workspace key grants access to this token-rotation endpoint; keep that key
private. No broker order endpoint is enabled. After-hours quotes may remain stale.

Fresh full-stack application. FastAPI + SQLite + browser frontend. No demo prices or generated market fixtures are loaded at runtime. This is research software with unvalidated signal rules, not a proven profitable system or broker order executor.

## Start

Python 3.11+ is required. Run inside this folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt
cp .env.example .env
python run.py
```

Windows: use `.venv\Scripts\activate`, and copy `.env.example` using your file manager. Open **http://127.0.0.1:8080** in your browser. Opening the HTML file directly will not run the backend.

The `.env` file is optional for historical research. For live Dhan data, set `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` privately in that file. Data subscription/permissions and network access must be available on your account. This build has not been tested against an authenticated Dhan account because no credentials were provided.

The Dhan adapter uses official v2 expiry-list, option-chain, quote-depth and one-minute candle APIs. Calls are spaced by at least 3.1 seconds. Automatic refresh is every 30 seconds within normal weekday session hours. Holidays/special sessions are not inferred; quote freshness and completed-bar checks remain required. Dhan's chain response does not include an exchange quote timestamp: nearby depth quotes use last-trade time as a conservative freshness proxy. This limitation is visible in the UI. An inactive contract may be rejected even when its book is current.

## Implemented workflows

- Actual completed-candle chart and live/imported mode separation.
- Price structure, EMA 9/21 and opening-range research rules.
- Completed 15-minute structure and prior-day structure from four full prior index sessions. The Dhan adapter fetches 14 calendar days of minute index history once per instance/day and today's minutes separately. If prior history is missing, the daily signal remains UNKNOWN. Market holidays are represented by absent bars, not invented trading sessions.
- Fixed-contract, same-source, same-session 5–10 minute ΔOI comparison with option midpoint changes. Three paired strikes required; no claim to identify investor intent.
- Separate price and option evidence; combined agreement, conflict and missing-data states.
- Server-side capital, risk, spread, quote age, lot-size and displayed-depth checks.
- Saved research plans with immutable inputs and timestamps, idempotent journal insertion, formula-safe CSV export.
- Historical JSON import, quote-based replay, next-observation fills, fixed expiry/strike/security identity, session isolation, slippage, per-lot costs, unresolved exits and persisted experiment JSON.
- Dataset SHA-256 and rule version recorded for reproduction.
- Sourced context records with publication-time filtering and stale/missing labels.

## Import data

Use **Data connections → Download input schemas**. The API accepts JSON and rejects unknown fields, invalid OHLC, duplicate observation timestamps, non-finite numbers and naive timestamps. Maximum request size: 12 MiB. One dataset: at most 30,000 candles and 5,000 option snapshots. This is a bounded research tool, not an unlimited tick-history warehouse.

### Historical dataset shape

```text
{
  name: string,
  provenance: string,
  candles: [{timestamp, open, high, low, close, volume}],
  snapshots: [{timestamp, symbol: "NIFTY", spot, source,
    contracts: [{security_id, expiry, strike, side: "CE" | "PE",
      lot_size, bid, ask, bid_qty, ask_qty, oi, volume, iv, delta, quote_at}]}]
}
```

Supply real numbers from your authorised data source. `timestamp` and `quote_at` are ISO-8601 with explicit offsets. Candle timestamps mark the START of each one-minute candle; completed 5-minute bars require all five constituent minutes. `expiry` is `YYYY-MM-DD`. `volume` is cumulative daily contract volume for options. `quote_at` is the timestamp of the observed quote. Do not set it to the import time or relabel a last price as bid/ask.

Imported prices are not independently verified. Provenance is displayed and preserved; importing synthetic data for your own tests will not make it a live feed or verified market evidence.

### Context records

Upload an array with `key`, `label`, `value`, `source`, `observed_at`, `published_at`, `valid_until`, optional `note`. Supported keys:

`fii_cash`, `dii_cash`, `fii_derivatives`, `us_markets`, `asia_markets`, `gift_nifty`, `usd_inr`, `dollar_index`, `us_yield`, `crude`, `gold`, `copper`, `india_vix`, `breadth`, `events`.

Records published after a historical decision are excluded. Context values are displayed separately; **this version does not include these inputs in the signal rules or claim automatic global/FII feeds**. It does not parse headlines or infer participant intent.

### Contract master

Upload an array with `security_id`, `expiry`, `strike`, `side`, `lot_size` using your current provider/exchange master. A metadata import replaces the active map. Dhan contract IDs must match all identity fields before a lot size is attached. No exchange lot sizes are guessed.

## Backtest interpretation

Backtests run on supplied option bid/ask observations. The signal-time contract is fixed; entry uses its NEXT observation within 60 seconds. This is a sampled-quote simulation, not a tick-by-tick fill guarantee. Size is limited to displayed ask quantity. Exits need sufficient displayed bid quantity. Observed stop gaps can exceed the planned loss.

Session changes, missing contract history, or observation gaps exceeding 120 seconds may yield unresolved trades. Result status becomes `INCOMPLETE`; P&L and drawdown explicitly cover resolved trades only. Intrabar stop touches between snapshots are unknown. The system does not fill stops at an ideal price or assume unknown exits were winners.

Each experiment has a fixed route: `chart`, `options`, or `combined` agreement. Time-based lead/lag attribution and multi-strategy selection are not claimed. Costs/slippage are user assumptions. Limit is three entries per session; new entries stop after realised session loss reaches twice the configured per-trade risk. No new entries are signalled from 15:00 IST; available active positions exit from 15:20 IST. Unknown losses cannot be treated as realised gains.

The `trend_combined` research route additionally requires the chart direction, completed 15-minute direction, four prior complete sessions' daily direction, and fixed-strike option pattern to agree. Historical replay uses only candles ending by the decision timestamp and excludes the current day from prior-day structure. `NO_ELIGIBLE_TRADES` means no performance can be estimated; the result reports assessed snapshots, eligible signals and trend-ready snapshots. The default `combined` route remains unchanged. The live Trading Room shows both decisions separately. An index candle history alone cannot backtest option entry or exit: a dataset must include historical option quotes, bid/ask sizes, identifiers, expiries and valid quote timestamps. Provider live snapshots collected while the app is running are not a historical options archive.

Run the same rules on a separate held-out dataset. A profitable test is not independent evidence of future profitability.

## Storage and access

SQLite defaults to `data/theta.sqlite3`, override with `THETA_DB_PATH`. Back up the stopped application's data folder, including SQLite WAL files if present. No automatic purge or cloud backup is installed. Monitor disk use during collection.

Default listener is loopback. `run.py` refuses a non-local listener without `THETA_API_KEY`. Remote deployment requires a private HTTPS reverse proxy, a strong API key, and an explicit `THETA_ALLOWED_HOSTS` list. The frontend accepts only the workspace key, in memory for the tab; broker credentials stay server-side. No public deployment was performed. This is single-user software, not multi-tenant account management.

## Verification

```bash
python -m pytest -q tests
```

Tests cover no-data behaviour, import validation, persistence, chart/option analysis, next-observation entry, contract rollover, stop slippage, stale/missing data, future-publication exclusion, API access boundaries, journal deduplication and CSV sanitisation. All test market fixtures are explicitly synthetic and live only under `tests/`.

## Remaining external setup

1. Run on your computer/server and configure authorised market-data credentials.
2. Import the current contract master and actual historical bid/ask data.
3. Connect or import each desired institutional/global source with timestamps.
4. Validate strategies using out-of-sample and live paper observations.

Do not call this live-ready until those connections and deployment checks are completed.

## Setup readiness

1. On Google Cloud Shell upload the newest ZIP, unzip it and run `bash deploy/update-trading-room.sh` inside its `app` directory. The script updates existing Cloud Run source while preserving configured secrets and storage.
2. During the next open Indian index session, inspect Data connections and Trading Room. A provider connection, fresh completed candles and fresh option quotes must all be present; a received WebSocket packet with an unknown trade timestamp does not establish a fresh quote.
3. Historical trend needs four complete prior trading sessions. The adapter asks Dhan for 14 calendar days of index minute bars. An empty/unsupported response leaves the daily gate UNKNOWN.
4. Backtesting needs a separate authorised dataset of timestamped options bids, asks and depth. Upload it via Data connections. Run `trend_combined` in Strategy lab and compare on a separate held-out dataset. Review unresolved fills and actual costs before making any performance claim.
5. US/Asian market, institutional flows and macro context remain unconnected to the strategy. Do not treat toolbar values as verified strategy inputs. Broker order placement is disabled.

## Google Cloud / tablet hosting

See [deploy/GOOGLE-CLOUD.md](deploy/GOOGLE-CLOUD.md) for the prepared Cloud Run
setup, private persistent storage, workspace authentication and exact deployment
status. The cloud script defaults to collection while the dashboard is open;
it does not silently enable an always-on paid collector.

## Market watch toolbar

Upper toolbar provides touch scrolling and All / India / Asia / Commodities /
Currency / US filters. Dhan market quotes are requested per segment, at most once
per 30 seconds in an instance; an open visible page refreshes the toolbar every
60 seconds independently of the NIFTY session. MCX futures may trade at different
hours. This is a snapshot display, not tick streaming or a background archive.

The official detailed master is downloaded on demand with a six-hour in-memory
cache and refreshed on a new IST date. Exact symbols and segments resolve NIFTY,
Bank Nifty, Sensex, VIX, GIFT Nifty, standard MCX Gold/Silver/Crude/Natural Gas/Copper
and NSE USDINR futures when current contracts exist. Earliest non-expired contracts
are labelled with expiry. MCX futures are not international USD spot commodities.
NIFTY option lot metadata is resolved from exact option identities in the master.

Price change uses Dhan's net_change and its implied previous close, not the day
OHLC close. Fetch time and last trade time are distinct. Missing quote timestamps
are never called live; old quotes age to stale, including after service restarts.
Nikkei, Hang Seng, Shanghai, KOSPI and US indices need a separate authorised feed.
The toolbar does not silently feed prices into the unvalidated trading strategy
or alter historical backtest context. Market context cards remain imports.

Update the existing deployment with `bash deploy/update-toolbar.sh`. It preserves
existing credential bindings. Verify `Refresh markets` after unlocking the app.

## Live toolbar (v6)

Authenticated GET /api/markets/live relays Dhan v2 ticker WebSocket packets as
streamed NDJSON. Tokens stay on the server. Browser updates arrive from provider
events, with render batching at 150 ms. The fixed three-row layout is retained.
Previous close packets drive change calculations. Unsupported Asian/US feeds
are not invented. Chart and options analysis still use their existing REST cycle.

Each visible browser tab uses one upstream connection, with at most three relays
per instance. Dhan allows five connections per account including other tools;
provider rejection codes appear in the feed state. The browser pauses when hidden
and reconnects with backoff after errors. Connections recycle after 15 minutes
for catalog expiry refresh. Cloud Run request timeout is set to 20 minutes by
the update script; active long-running requests can increase runtime charges.

Connected does not mean every quote is fresh: last-trade age remains explicit.
No ticks are simulated during closed sessions. In-memory streaming ticks are for
the toolbar only, not an audit-quality tick archive or backtest dataset.

## Trading room v7

New Trading room page combines a self-hosted TradingView Lightweight Charts 5.2.1
candlestick chart, Call and Put contract inspectors, evidence, and links to the
existing risk planner and journal. The room adapts to tablet width: chart above
the two option panels on narrower displays. Library license and NOTICE are
included and linked from the chart. The bundle comes from the official npm
`lightweight-charts` package; no private TradingView API or socket is used.

Candles remain completed five-minute bars from the current backend response.
Dhan WebSocket NIFTY ticks update a separate dashed price line and price label.
No partial candle is fabricated from incomplete tick coverage. Selected historical
datasets exclude live ticks. Option panels display REST snapshots, not streaming
option depth. Exact contract selection passes to the existing risk planner.
Automatic orders, TradingView Pine execution and strategy validation are not added.

Deploy this package with `bash deploy/update-trading-room.sh` in Google Cloud Shell.

## Dhan data collection (Data connections)

The in-app Dhan data section fetches and durably stores source payloads, with a visible
row count and a JSON download for each saved record. Select a date range of at most
30 calendar days and request NIFTY/BANKNIFTY index candles, the exact current
futures contract (if present in the current Dhan instrument master), or one expired
option relative strike/side. "Fetch ATM ±5 CE/PE" makes 22 sequential option requests
for the selected range; keep the tab open until it finishes. Repeat for earlier
ranges. Requests with zero rows are recorded as zero, never backfilled. The
live-chain button saves one nearest-expiry snapshot; the Start button continues
at a 10-second interval only while the Data connections tab remains open and
connected. Cloud Run does not promise background collection after the tab closes.

Dhan does not provide historical full option-chain snapshots or historical
bid/ask in rolling options. Relative ATM option candles can change strike during
a hold. Active futures are not a continuous futures history. Dhan does not supply
international spot indices or FII/DII through these endpoints. Historical source
payloads are kept apart from strategy datasets until contract identity and fills
are independently validated. No order endpoint is used.
