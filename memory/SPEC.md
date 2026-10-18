# SPEC — NIFTY PCR + OI + VWAP Signal System (V1)

**STATUS: Phase A–J complete. PRODUCTION_READY = NO (external inputs PENDING). LIVE = OFF, ARMED = OFF.**

## Phase J — external-input verification harness (current phase)
Verification only; the frozen strategy was not touched. External inputs were NOT
supplied in this environment (`DHAN_ACCESS_TOKEN`, `DHAN_CLIENT_ID`,
`DHAN_INSTRUMENT_MAP_PATH`, `REAL_HISTORICAL_PATH`, `TELEGRAM_*` all ABSENT), so
every credential/dataset-dependent item reports **PENDING** — nothing was faked.
- `research/parity.py` — §13 LIVE/REPLAY parity: `live_path_signals()` mirrors the
  live pipeline's engine call sequence (same `build_features` + `evaluate_and_apply`,
  one SignalMemory per session) and is diffed field-by-field against
  `replay_signals()` over ATM, total PCR, PCR trend, put/call OI, VWAP,
  above/below VWAP, CE/PE score, decision, state, data health. A tamper test
  proves the harness detects a divergence.
- `research/audits.py` (extended) — §7 `audit_mutation_leakage()`: distorts
  price/candle/OI/PCR/option-LTP/volume for every snapshot AFTER T and requires
  all decisions at or before T to stay bit-identical; §8
  `audit_determinism_runs()`: 3 runs ⇒ one replay hash + identical metrics.
- `research/oos.py` — §10 chronological 60/20/20 split on whole trading days
  (no shuffle, no tuning); < 15 genuine trading days ⇒ `status = PENDING` and no
  split is fabricated. Each window is evaluated by the existing replay engine.
- `execution/instruments.py` (extended) — §3 `validate_instrument_map()` /
  `instrument_map_status()`: required fields (security_id, exchange_segment,
  instrument_type, expiry, strike, option_type), valid Dhan segments/instrument
  types, canonical coverage (NIFTY, NIFTY FUTURE, NIFTY CE, NIFTY PE); absent ⇒
  PENDING, corrupt/partial ⇒ FAIL; `broker_verified` is always false until a real
  instrument-master lookup runs. The SAME `InstrumentMap` object serves data,
  replay, paper, execution and reconciliation.
- `research/analytics.py` (extended, additive fields) — §11 finer descriptive
  buckets: `by_score_bucket_fine` (35-44…85-100) and `by_time_of_day_30m`
  (11:30-12:00 … 15:00-15:15). Phase I buckets unchanged.
- **TELEGRAM_REQUIRED = FALSE** (operator decision): the checklist item is
  `ALERTS (TELEGRAM)` with status `OPTIONAL`; Telegram is excluded from
  `MANDATORY_GATES` and can never block `production_ready`. Readiness now reports
  `optional`, `mandatory_gates` and `mandatory_pending`, and
  `production_ready = no FAIL and no mandatory PENDING`.
- Readiness checklist grew to **19 items** (added INSTRUMENT MAP VALIDATION,
  DETERMINISM, LIVE/REPLAY PARITY, IDEMPOTENCY, OOS VALIDATION).
- API (read-only, all 409 + "PENDING" without a dataset):
  `GET /api/research/instrument-map`, `POST /api/research/parity`,
  `POST /api/research/leakage/mutation`, `POST /api/research/determinism`,
  `POST /api/research/oos`.
- Frontend: instrument-map + broker-verified rows added to the research card.
- Tests: `test_phase_j_validation.py` (29) incl. risk-gate condition coverage,
  kill-switch retry-bypass resistance, idempotency stability, repo+log secret
  scan, `.env.example` placeholder-only check, and PENDING-honesty assertions.

## Phase I — real-data validation & production readiness (current phase)
Validation ONLY. Nothing in `research/` can touch a strategy value — it reads
snapshots and Phase E replay output. No optimization/tuning/ML/indicator added.
- `research/ingest.py` — real dataset ingestion through the EXISTING Phase E
  sources (CSV/JSON; parquet refused rather than adding a dependency). Required
  CSV columns are validated; a missing required field raises `RealDataError`
  ("NO_DATA") and is NEVER inferred. Content-addressed `dataset_hash` (sha256 of
  sorted per-snapshot fingerprints) + `DatasetDescriptor` (dataset_id, hash,
  source, origin, date range, trading days, snapshot count, schema/feature/
  strategy versions) for reproducibility (§33).
- `research/quality.py` — §8 report: snapshot/day counts, expected vs missing,
  duplicate ids/timestamps, out-of-order, timestamp gaps + largest gap, missing
  strikes/OI/LTP, invalid values (negative/NaN/inf), expiry coverage,
  strikes-per-snapshot min/max. `production_quality` is true only when every
  threshold passes; failures are listed, never repaired.
- `research/audits.py` — leakage audit (§11: no feature read after the signal
  timestamp, no outcome input before it), session-reset audit (§12: per-day
  replay must equal the combined replay, VWAP restarts, no cooldown carryover)
  and determinism audit (§32: dataset hash order-independent, replay hash +
  metrics identical across runs).
- `research/analytics.py` — descriptive layer on top of the Phase E metrics
  engine: profit factor, expectancy, max drawdown (all with written
  definitions), avg/median outcome, signal duration, buckets by score
  (0-34/35-49/50-64/65-79/80-100), time-of-day window, CE vs PE, PCR regime,
  VWAP regime, MFE-MAE volatility regime, and daily/weekly/monthly P&L. Every
  bucket carries `n` and a denominator string; nothing is called accuracy or a
  probability. `build_evidence()` produces the §25 traceable record (features,
  rules, scores, decision, state, outcome, MFE/MAE, versions, origin).
- `research/validation.py` — orchestrator (dataset → quality → Phase E replay →
  audits → analytics → evidence), `dataset_status()` (PENDING without
  `REAL_HISTORICAL_PATH` / `DHAN_*`), `readiness_report()` (§30 14-item
  checklist, PASS/FAIL/PENDING) and `evidence_csv()` export.
- Data-origin labels never mixed: `REAL_HISTORICAL`, `LIVE_REAL`,
  `REPLAY_SYNTHETIC`, `LIVE_SIMULATED`.
- API (read-only): `GET /api/research/status`, `/readiness`, `/report`,
  `POST /api/research/validate` (409 + "PENDING" when no dataset; 409 "NO_DATA"
  on a broken schema), `POST /api/research/export/evidence` (CSV).
- Frontend `ResearchAnalyticsCard` on the control room: data-source statuses,
  dataset id/hash/date range, N signals + N resolved, gross/net points,
  drawdown, strategy/feature versions, the four audits, readiness checklist and
  the honesty footer.
- One additive ingestion fix in `storage/sources.py`: the CSV source now forwards
  the `expiry` column into the existing normalizer (no strategy code touched).
- Env: `REAL_HISTORICAL_PATH` (absent ⇒ REAL HISTORICAL VALIDATION = PENDING).
- Tests: `test_phase_i_real_data.py` (34). The CSV used there is a SCHEMA
  FIXTURE, not market data — genuine market validation stays PENDING.

## Phase H — live execution / risk infrastructure (current phase, DEFAULT OFF)
Purely ADDITIVE. Phases A–G (PCR/ATM/OI/VWAP/feature/rule/score/signal/state/
cooldown/replay/outcome/TradingView/alerts/paper) are byte-frozen except the
config schema. No AI/ML/optimization/indicator was added; scope stays NIFTY.
- **Verified Dhan v2 contract** (docs crawled, not guessed): `POST /v2/orders`
  with `access-token` + `client-id` headers; body `dhanClientId, correlationId,
  transactionType (BUY/SELL), exchangeSegment (NSE_FNO), productType (INTRADAY…),
  orderType (LIMIT/MARKET/STOP_LOSS…), validity (DAY/IOC), securityId, quantity,
  price`; status reads `GET /v2/orders/{order-id}` and
  `GET /v2/orders/external/{correlation-id}`; positions `GET /v2/positions`;
  broker statuses TRANSIT/PENDING/TRADED/PART_TRADED/REJECTED/CANCELLED mapped to
  the internal lifecycle (SUBMITTED ≠ FILLED).
- Flow: `SignalDecision -> RiskEngine -> can_execute_order() -> ExecutionService ->
  ExecutionAdapter -> Dhan`. `execution/dhan_execution.py` is the ONLY module that
  touches order endpoints (a test walks the tree to prove it); the market-data client
  `data/dhan_client.py` stays separate and order-free.
- Safety defaults (§2): `system_mode: RESEARCH`, `live_execution_enabled: false`,
  `live_execution_armed: false`, `execution_adapter: mock`, kill switch OFF.
  **Two-step arming** needs config flags AND env `LIVE_EXECUTION_ENABLED=true` +
  `LIVE_EXECUTION_ARMED=true`. §46: no auth system exists, so
  `control_enabled = false` — there is no UI/API route that can enable, arm, or
  submit anything (execution API is GET-only).
- `can_execute_order()` (execution/gate.py) evaluates, in order: mode, enabled,
  armed, kill switch, strategy-enabled flag, Phase C cooldown, decision is
  CE_SETUP/PE_SETUP (WAIT can never execute), signal state, data_health == OK,
  configured session, duplicate `client_order_id`, instrument + securityId +
  lot size + tradability, price sanity (0/neg/NaN/inf rejected), reconciliation
  state, then RiskEngine. Block reasons are deterministic constants
  (MODE_NOT_LIVE, LIVE_EXECUTION_DISABLED, LIVE_EXECUTION_NOT_ARMED, KILL_SWITCH,
  COOLDOWN, INVALID_SIGNAL, STALE_DATA, OUTSIDE_TRADING_SESSION, DUPLICATE_ORDER,
  SECURITY_ID_UNAVAILABLE, INSTRUMENT_INVALID, PRICE_INVALID, QUANTITY_LIMIT,
  SLIPPAGE_LIMIT, MAX_TRADES_PER_DAY, MAX_OPEN_POSITIONS, MAX_DAILY_LOSS,
  RECONCILIATION_MISMATCH, UNRECONCILED_POSITION).
- `RiskEngine`: explicit config only (`risk_max_trades_per_day/open_positions/
  quantity/daily_loss/slippage_points`); daily loss uses REALIZED broker-confirmed
  P&L only and locks execution; no capital/quantity inference; disabled risk fails
  CLOSED.
- `ExecutionService`: deterministic `client_order_id` from the signal id (≤30 chars,
  used as Dhan `correlationId`), one submission per signal, timeout → UNKNOWN →
  correlation-id reconciliation with NO blind retry, partial fills tracked
  (requested/filled/remaining/average_fill_price), EXECUTION_LOG + RISK_LOG entries
  with no secrets. Exit management reuses Phase C/E semantics and refuses to exit a
  zero/unknown/unreconciled position.
- `PositionReconciliationService`: broker is authoritative; startup reconciliation
  runs before live execution is permitted and stays PENDING (blocking) when the
  broker is unconfigured; an unexpected broker position → MISMATCH → new entries
  blocked.
- Frontend: `ExecutionStatusCard` (mode, live/armed/kill-switch/risk/broker/
  reconciliation, limits, pre-arm checklist, control-disabled note). Display only —
  no BUY/SELL/EXECUTE/ARM control anywhere; the frontend never talks to Dhan.
- API (GET only): `/api/execution/status`, `/log`, `/submissions`, `/reconciliation`.
- Real Dhan authentication / real order verification: **PENDING** (no credentials);
  `MockExecutionAdapter` is used everywhere in tests — no real order was ever sent.
- Tests: `test_phase_h_execution.py` (53) incl. the full §52 zero-broker-call matrix.

## Phase G — alerts + PAPER trading (current phase)
NO BROKER EXECUTION. Both features sit strictly DOWNSTREAM of the frozen
Phase A–F strategy; no PCR/OI/VWAP/rule/score/signal/state/cooldown/replay/
TradingView code was modified.
- Modes: `system_mode` = RESEARCH (default) | PAPER. LIVE EXECUTION = NOT IMPLEMENTED.
  RESEARCH runs paper simulation only when `paper_trading_enabled: true` (currently on
  for research); PAPER enables paper trading + alerts. Broker orders: zero in both.
- `backend/alerts/provider.py` — `AlertProvider` protocol (+ `NullProvider`), so another
  channel needs no change elsewhere. The signal engine never imports `alerts/`.
- `backend/alerts/telegram.py` — `TelegramProvider`: env-only `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID`; absent => `configured=False` => NOT_CONFIGURED and the engine
  continues; `redact()` scrubs token/chat-id (and token halves) from every error.
- `backend/alerts/formatter.py` — deterministic message text (spot, backend VWAP +
  distance, PCR/ATM PCR/trend, CE/PE OI + change, `X/100`, state, reason, mode,
  versions, data-origin label). No %, "guaranteed", "sure shot" or probability claim.
  `alert_type_for()` maps CE_SETUP/PE_SETUP/INVALIDATED only — WAIT is never alerted.
- `backend/alerts/service.py` — `AlertService.dispatch(marker)`: dedupe on
  (signal_id, alert_type), ALERT_LOG records (`alert_id, signal_id, timestamp,
  provider, alert_type, status, error, message_preview`) that never hold credentials,
  and total failure containment (`ALERT_FAILED` + continue).
- `backend/paper/paper_engine.py` — `PaperTradingEngine`: entry from the backend ATM
  option LTP (missing price => NO_DATA, never invented), target/stop from the EXISTING
  Phase C `target_points`/`stoploss_points`, invalidation from the BACKEND decision,
  same-observation target+stop => AMBIGUOUS (Phase E semantics, reusing
  `replay.outcome_engine.OptionObservation`), expiry after `market_close`, one position
  per signal and `max_open_paper_positions` (default 1). P&L: gross_points, gross_pnl,
  assumed slippage, costs, net_pnl — labeled `PAPER / HYPOTHETICAL — not broker execution`.
- `backend/models/{alert_models,paper_models}.py` + `frontend/src/lib/paperTypes.ts` —
  the mirror pairs. States: OPEN/TARGET/STOPLOSS/INVALIDATED/EXPIRED/NO_DATA/AMBIGUOUS
  (no broker state exists).
- Pipeline: after each decision the marker goes to `alerts.dispatch` and
  `paper.on_tick` inside separate try/except blocks — an alert or paper failure is
  logged (`ALERT_FAILED` / `PAPER_ERROR`) and the engines keep running.
- API (read-only GET only): `/api/paper/positions`, `/api/paper/positions/{id}` (404),
  `/api/paper/trades` (PAPER_TRADE_LOG), `/api/paper/summary`, `/api/alerts` (ALERT_LOG
  + status), `/api/alerts/status`. No POST/PUT/DELETE, no order route.
- Frontend: `PaperTradingCard` on the control room (mode badge, open position entry/
  current/target/SL/points, counts, net paper P&L, PAPER/HYPOTHETICAL label, Telegram
  status) and `PaperLevelsPanel` on `/chart` (entry/target/SL/exit for the selected
  signal). No BUY/SELL/EXECUTE control anywhere.
- Config: `system_mode`, `alerts_enabled`, `alert_wait_alerts` (false),
  `paper_trading_enabled`, `max_open_paper_positions`, `paper_quantity`,
  `paper_slippage_points`, `paper_costs_per_trade`.
- Telegram real delivery: **PENDING** (no credentials configured; `real_delivery_verified`
  is always false until a live send is proven).
- Tests: `test_phase_g_alerts.py`, `test_phase_g_paper.py`, `test_phase_g_integration.py`
  (46 total). One pre-existing Phase D assertion was TIGHTENED,
  not weakened: `test_no_order_endpoints_exposed` now still bans order/execute/webhook
  paths and additionally requires every "position" route to be GET-only under `/api/paper`.

## Phase F — TradingView chart + signal visualization (current phase)
VISUALIZATION ONLY. No strategy, formula, weight, cooldown or state change was made
(Phase A–E logic untouched); the chart never computes a signal.
- Authority: **TradingView VWAP = visual reference only; backend VWAP = strategy-authoritative.**
  PCR/OI/score/decision/state/invalidation are all backend-produced and only serialized.
- `backend/models/chart_models.py` — `ChartSignalMarker` (signal_id, IST timestamp, decision,
  state, marker_kind SETUP/INVALIDATION/WAIT, side, spot, backend vwap + distance, pcr,
  atm_pcr, pcr_trend, atm, ce/pe OI + change, score + max_score ("X/100", never %),
  reason, data_health + stale, strategy/feature/config versions, data_origin +
  data_origin_label, origin_signal_id, invalidation_timestamp/reason) and
  `ChartSignalsResponse` (symbol NIFTY, tradingview_symbol NSE:NIFTY, timeframe "5",
  Asia/Kolkata, latest decision/state/spot/vwap/health, notes).
- `backend/chart/markers.py` — pure serialization of `SignalDecision` (live) and Phase E
  `ReplaySignal` (replay); `link_invalidations` attaches each invalidation to the preceding
  setup's signal_id and NEVER deletes/mutates the historical setup marker;
  `visible_markers` hides WAIT unless `include_wait=true`. Contains no rule/score/state import.
- `backend/routers/chart_router.py` — `GET /api/chart/signals?origin=LIVE|REPLAY-SYNTHETIC&
  include_wait=&limit=`, `GET /api/chart/signals/{signal_id}` (404 when unknown),
  `GET /api/chart/latest`. LIVE markers come from `Pipeline.chart_markers` (a 500-entry
  deque appended once per tick); REPLAY-SYNTHETIC reuses the EXISTING Phase E
  `run_replay(InMemorySource(synthetic_session()))` (cached, deterministic) — no second
  replay engine.
- Origin labels (§8): `LIVE • SIMULATED DATA` and `REPLAY • SYNTHETIC DATA`. "LIVE MARKET"
  is never displayed — real Dhan data is not configured.
- Frontend `/chart` route (`frontend/src/pages/Chart.tsx` + `components/chart/*`,
  mirror types in `src/lib/chartTypes.ts`): TradingView Advanced Chart widget embed
  (NSE:NIFTY, 5m, dark, Asia/Kolkata, STD;VWAP study, no API key) with a graceful
  "TradingView unavailable — backend signal data remains available" fallback; controls
  (NIFTY only, 5m, LIVE SIM / REPLAY-SYNTHETIC, marker policy, Show WAIT OFF by default);
  backend signal overlay list; click-to-open marker detail panel with every audit field;
  latest-signal strip incl. backend VWAP; STALE DATA badge; "Signal data unavailable" on
  API failure; no horizontal overflow at 390px. Control room links to it
  (`nav-chart-link`); the control room itself was NOT redesigned.
- Known limitation: the free TradingView widget may report "symbol only available on
  TradingView" for NSE:NIFTY index data depending on region/data entitlement — a
  widget-side limitation surfaced in the UI note; backend data is unaffected.
- Tests: `backend/tests/test_phase_f_chart.py` (17).

## Phase E — historical snapshots + replay + backtest validation (current phase)
VALIDATION ONLY: the frozen A–D strategy is evaluated as-is. No optimization, tuning,
ML, parameter search or indicator discovery exists (audited).
- `backend/storage/snapshot_store.py` — immutable SQLite repo
  (`REPLAY_DB_PATH`, default `backend/data_store/snapshots.db`): raw payload and derived
  features in separate columns; duplicate `snapshot_id` is detected + logged + skipped
  (first write wins, no random replacement ids); session-date indexed loading.
- `backend/storage/sources.py` — `HistoricalDataSource` protocol +
  `InMemorySource`, `SqliteSource`, `JsonFileSource`, `CsvFileSource` (flat one-row-per-strike CSV).
- `backend/replay/replay_engine.py` — `ReplayFeed` (deterministic sort by
  (timestamp, snapshot_id); `replay_strict_ordering=true` REJECTS out-of-order input with
  `ReplayOrderingError`) and `replay_signals()` which reuses `build_features` +
  `evaluate_and_apply` — no separate backtest strategy. No look-ahead: bars/PCR history/
  prev-snapshot accumulate strictly forward; one `SignalMemory` per session date so
  cooldown matches live and sessions never share VWAP/PCR. `replay_hash()` = SHA-256
  determinism checksum.
- `backend/replay/outcome_engine.py` — outcomes from ACTUAL future option LTP (CE leg for
  CE setups, PE leg for PE setups) at the signal's ATM strike; target/stop from the
  existing `target_points`/`stoploss_points`; MFE = max(ltp-entry), MAE = min(ltp-entry);
  states TARGET/STOPLOSS/INVALIDATED/EXPIRED/AMBIGUOUS/NO_DATA. Same-observation
  target+stop reachability => AMBIGUOUS (never the favorable side). Missing data =>
  NO_DATA (no invented exit). Invalidation from the replayed state machine wins over a
  later touch.
- `backend/replay/metrics.py` — counts, avg/median MFE & MAE, avg time-to-target/stop,
  gross points, `target_rate_of_resolved = TARGET/(TARGET+STOPLOSS)` with the denominator
  spelled out (never called accuracy); segments by side (CE/PE separate), configurable
  time windows, PCR regime, VWAP regime (`vwap_near_points`); data-quality stats incl.
  signals suppressed by bad data; cost model labeled HYPOTHETICAL when zeros;
  `daily_report`, `range_report`, and `signals/outcomes/metrics` CSV exporters.
- `backend/replay/backtest_engine.py` — `run_replay(source, ...)`; tags results
  SYNTHETIC vs HISTORICAL and attaches the "not market performance" note for synthetic runs.
- `backend/replay/fixtures.py` — deterministic SYNTHETIC sessions (no RNG) used to prove
  replay correctness.
- `backend/models/replay_models.py` — `ReplaySignal` (full audit trail incl. 12 RuleResult
  dumps + versions), `SignalOutcome`, `SegmentStats`, `DataQualityStats`, `CostAssumptions`,
  `ReplayMetrics`, `ReplayResult`; `backtest_methodology_version = "1.0.0"` on every outcome.
- API (read-only, offline): `POST /api/replay/run`, `POST /api/replay/report/daily`,
  `POST /api/replay/report/range`, `POST /api/replay/export/{signals|outcomes|metrics}`
  (CSV), `GET /api/replay/snapshots/status`. `source=sqlite` with no stored data returns
  409 "PENDING HISTORICAL DATA" rather than fabricating results.
- Config: `replay_enabled`, `replay_include_pre_signal_data`, `replay_strict_ordering`,
  `backtest_brokerage_per_trade`, `backtest_slippage_points`, `backtest_charges`,
  `backtest_quantity`, `backtest_time_windows`, `vwap_near_points`.
- Phase D dashboard was NOT redesigned (§37).

## Phase D — Google Sheets control room + dashboard
- `backend/pipeline.py` — the ONLY live loop: feed -> normalizer -> feature engine ->
  signal engine -> dashboard state + batched Sheets queues. 5s tick loop + 30s flush
  loop, both started in `server.py`'s lifespan; every failure is caught and logged
  (a feed timeout or Google outage never touches the engines). `SimFeed` (labeled
  simulation) and `DhanFeed` (read-only, needs credentials) share one interface.
- `backend/sheets/google_sheets.py` — Sheets v4 adapter: service-account JWT ->
  OAuth token (cached), timeouts, retry with backoff on 429/5xx, `redact()` so no
  key/token can ever reach a log. DISABLED unless `GOOGLE_SHEETS_ID` +
  `GOOGLE_SERVICE_ACCOUNT_JSON` are set.
- `backend/sheets/writers.py` — per-tab queues + throttled batch flush: ONE HTTP call
  per tab per flush (never per row/tick). Tabs: RAW_DATA (one row per ATM-window
  strike), PCR_DATA, SIGNAL (WAIT decisions logged too), TRADE_LOG (first row is the
  `PAPER / RESEARCH ... NOT BROKER EXECUTION` banner; no broker-order-id column
  exists), SYSTEM_LOG. Headers written once.
- `backend/sheets/settings.py` — SETTINGS tab -> typed, range-checked config merged
  through the same `AppConfig` validators. Editable: MARKET_OPEN, SIGNAL_START,
  SIGNAL_END, ATM_RANGE, PCR_LOOKBACK, MIN_SCORE, TARGET_POINTS, STOPLOSS_POINTS,
  SIGNAL_COOLDOWN, SYSTEM_ENABLED (may only DISABLE). Immutable from the sheet:
  INDEX, TIMEZONE, STRATEGY_VERSION, FEATURE_ENGINE_VERSION, rule weights. Invalid
  values are rejected (SYSTEM_LOG WARN) and the previous config is kept.
- `backend/sheets/dashboard.py` — `build_dashboard(...)` is the SINGLE mapping feeding
  both `GET /api/dashboard` and the DASHBOARD sheet (`dashboard_grid`). Display law:
  labels only "CE SETUP"/"PE SETUP"/"WAIT", scores "X/100" (never %), statuses
  OK/WARNING/STALE/ERROR/DISABLED, stale never hidden, DISABLED when kill switch off.
- `backend/models/dashboard_models.py` + `frontend/src/lib/dashboardTypes.ts` — the
  Pydantic <-> TS mirror pair for the payload.
- Frontend control room (`frontend/src/pages/Home.tsx` + `components/dashboard/*`):
  dark tactical terminal (JetBrains Mono + IBM Plex Sans), TanStack Query polling
  `/api/dashboard` every 5s, shell renders unconditionally (static-preview safe).
  Panels: header/system, market strip, signal hero (scores + confirmed/unavailable/
  reason), PCR, OI, ATM option, data health, settings, system log.
- API (read-only): `GET /api/dashboard`, `GET /api/system-log`, `GET /api/sheets/status`,
  `POST /api/pipeline/tick`.
- Phase D config knobs: `pipeline_interval_seconds` 5, `data_source` sim|dhan,
  `sheets_flush_seconds` 30 (min 5), `sheets_enabled`.

### Bugs found & fixed during Phase D integration (documented per §19)
1. `data/validators.py::snapshot_health` classified the staleness message a second
   time as `PRICE: INVALID`, escalating a STALE feed to overall ERROR. Smallest fix:
   skip the DATA_AGE issue when absorbing price issues. Regression test:
   `test_phase_a_data.py::test_stale_snapshot_reports_stale_not_error`.
2. `sheets/dashboard.py::dashboard_grid` read ATM LTPs from the OI panel (AttributeError)
   — now reads `payload.option`. Covered by `test_dashboard_grid_mirrors_payload_for_sheets`.
3. `sheets/writers.py` had no TRADE_LOG entry in `TAB_HEADERS` (KeyError on flush) —
   added. Covered by `test_trade_log_is_marked_paper_research`.
4. `data/sim_feed.py` stamped ticks with the bar's minute timestamp, making every
   simulated snapshot up to 60s stale vs `max_data_age_seconds: 10` (dashboard stuck
   on STALE). Sim-harness-only fix: stamp the quote as of `at`. No threshold or
   strategy change.
NOTE: no Phase A/B/C strategy logic (PCR/VWAP/OI formulas, weights, thresholds,
cooldown, state machine, invalidation) was modified in Phase D.

## What the app does
Deterministic research/signal engine for NIFTY combining PCR (+ trend), OI (+ change),
VWAP, price structure, a time filter and a data-health gate into exactly three outputs:
`CE_SETUP` / `PE_SETUP` / `WAIT` — always with a full, auditable explanation.
No single indicator (incl. PCR) can produce a signal; the decision needs
`score >= minimum_score` (default 70/100) from six weighted rules.

## Data model (backend/models/)
- `market_models.py` — `InstrumentTick`, `OptionQuote`, `OptionChainRow`, `OptionChain`,
  `MarketSnapshot`, `DataHealth` (frozen Pydantic v2). Ids: uuid4 strings if persisted later.
- `feature_models.py` — `MarketFeatures` + `AtmFeatures`/`PcrFeatures`/`OiFeatures`/
  `VwapFeatures`/`PriceFeatures`; `TrendState` UP/DOWN/FLAT/INSUFFICIENT_DATA.
- `signal_models.py` — `RuleResult` (rule_id, side, passed, available, score_awarded,
  max_score, observed_value, reason) and `SignalDecision` (immutable, carries both
  rule lists, scores, reasons, versions, data_health, cooldown_until).
  **Score is NOT a probability — always "82/100", never "82%".**

## Engines (backend/engines/ — pure, no I/O, no clock; live/replay-shareable)
- `atm_engine` — half-up rounding to strike interval; `select_strikes` ATM±3 (7 strikes).
- `pcr_engine` — total + ATM-range PCR; zero-denominator/missing-strike => quality
  OK/INCOMPLETE/INVALID, never a crash; `pcr_change`; `pcr_trend_from_history`
  (configurable lookback=5, flat_band=0.02; single tick never classifies).
- `oi_engine` — raw OI + changes vs previous snapshot; deterministic interpretation labels.
- `vwap_engine` — session VWAP from OHLCV bars (single formula for live AND replay);
  zero-volume falls back to mean typical price; at spot==VWAP neither flag is set.
- `price_engine` — short-term direction (±flat-band %), recent high/low, structure state.
  No new indicators in V1.
- `feature_engine` — snapshot + prev snapshot + PCR history + bars -> `MarketFeatures`
  (version `1.0.0`), aggregating `DataHealth` (DATA_AGE/OPTION_CHAIN/PCR/VWAP/OI).
- `rule_engine` — six rules per side with explicit definitions:
  PCR_CONFIRMATION (trend UP for CE / DOWN for PE), VWAP_CONFIRMATION (spot > / < VWAP),
  OI_SUPPORT (ATM-range put>=call for CE / call>=put for PE), OI_CHANGE_SUPPORT,
  ATM_DATA_VALID, PRICE_CONFIRMATION. Missing evidence => available=false, 0 points.
- `score_engine` — sums RuleResults; NO score normalization (80 achievable = 80/100).
- `state_machine` — WAIT->WATCH->ENTRY_CANDIDATE->ACTIVE; exits
  INVALIDATED/TARGET/STOPLOSS/EXPIRED (ACTIVE = research state only); illegal
  transitions raise `IllegalTransitionError`.
- `signal_engine` — PURE `evaluate(features, config, state_snapshot, now)`; decision
  order: kill switch -> data-health gate -> time filter (11:30 start; 15:15 end,
  inclusive by default; naive datetimes treated as IST) -> invalidation (ACTIVE:
  VWAP cross and/or PCR reversal >= 0.15, both config-gated) -> cooldown ->
  conflict (both qualify => WAIT/CONFLICTING_SIGNALS) -> single-side setup -> WAIT
  with explanation. `evaluate_and_apply` separates mutation into `SignalMemory`.
- `signal_memory` — in-memory runtime state (state, side, signal ts, cooldown_until,
  signal_pcr, last decision); NOT persisted (later phase). Emissions start cooldown
  (default 10 min, window [T, T+10)); emissions also drive the ramp; cooldown
  suppresses same-side AND opposite-side setups (opposite needs explicit invalidation).
- `fixtures.py` — deterministic scenario builders (strong_ce/strong_pe/weak/conflict/
  stale/invalid_pcr/invalid_vwap/chain_incomplete/ce_below_vwap/pe_above_vwap).

## Data layer (backend/data/)
- `dhan_client.py` — READ-ONLY Dhan market-data client (index quote + option chain);
  env-only credentials (`DHAN_ACCESS_TOKEN`, `DHAN_CLIENT_ID`); no order methods by
  construction (enforced by `test_dhan_client_is_readonly`).
- `sim_feed.py` — clearly-labeled SIMULATED deterministic feed (harness only).
- `normalizer.py` / `validators.py` — payload -> models; staleness/validity/completeness
  checks that report instead of crash.

## Config (backend/config/settings.yaml — validated in lib/config.py)
Weights must sum to exactly 100; invalid config raises `ConfigError` (tested:
weights sum, minimum_score bounds, HH:MM format, NIFTY-only instrument,
data_source, flush interval). Key values: minimum_score 70, cooldown 10,
lookbacks 5/5, target 35 / SL 30 points (research parameters only),
strategy_version "1.0.0", config_version "1.0".

## API (all under /api via api_router; read-only)
- `POST /api/signal/evaluate` `{scenario, at_time?, use_memory?}` -> `SignalDecision`
- `GET /api/signal/state`, `POST /api/signal/reset` — in-memory harness state
- `GET /api/config` — effective validated config
- `GET /api/dashboard`, `GET /api/system-log`, `GET /api/sheets/status`,
  `POST /api/pipeline/tick` — Phase D control room
- legacy template routes: `GET /api/`, `POST/GET /api/status`

## Tests (backend/tests/, `cd /app/backend && pytest`)
330 total, all passing: Phase A 9, Phase B 18, Phase C 42, Phase D 48, Phase E 34,
Phase F 17, Phase G 46, Phase H 53, Phase I 34, Phase J 29
(storage roundtrip/duplicate immutability/raw-vs-derived separation, JSON+CSV+SQLite
sources, shuffled-input sorting, strict-ordering rejection, replay determinism (same
hash twice), session reset across days, pre-11:30 data collected but never signalling,
target/stop/ambiguous/no-data/expired/invalidation outcomes, MFE/MAE both sides,
cooldown preserved in replay, metrics denominators + segments, cost labeling, daily and
range reports, CSV auditability, audit trail with 12 rule results, three leakage tests
(future PCR / future price / future option LTP), and live-vs-replay parity).
Phase D coverage: settings validation incl. immutable-key rejection; dashboard
field-by-field mapping, signal/health/kill-switch mapping, DASHBOARD grid completeness,
display-law checks; Sheets batching — one call per tab, header-once, throttling, failure
isolation, retry on 429, redaction, credential-free rows; pipeline integration — full
payload, queueing, time filter, trend accrual, feed-failure containment, engines contain
no Sheets/execution imports, control-room endpoints, no order routes in the OpenAPI schema.

## Known limitations / next phases
- **REAL HISTORICAL DATA: PENDING.** Phase E replay is proven against deterministic
  SYNTHETIC fixtures only; those numbers are correctness proofs, NOT market performance.
  `POST /api/replay/run {"source":"sqlite"}` returns 409 until real snapshots are stored.
- **LIVE DHAN VERIFICATION = PENDING** and **LIVE GOOGLE SHEETS VERIFICATION = PENDING**:
  no credentials configured in this environment, so only local/simulated paths are
  verified. Adapters activate on adding env values; no live claim is made.
- No TradingView (Phase F), no Telegram (Phase G), no risk/execution architecture docs
  (Phase H). No execution path exists or is planned inside V1.
- TARGET/STOPLOSS/EXPIRED exits are reachable transitions in the live state machine; only
  the replay outcome engine classifies them today (live auto-detection is a later phase).
