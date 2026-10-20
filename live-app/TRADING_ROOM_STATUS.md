# Trading Room readiness — 3.7.0

The room is research software, not a validated entry recommendation.

## Fixed

- Current-session refresh passes the selected security ID into the shared setup evaluator.
- A response started before a contract/symbol/workspace change cannot replace newer setup evidence.
- Session refresh reuses the returned workspace instead of making a redundant full workspace request.
- Setup panel groups missing data, blocked rules, pending triggers and passed conditions. Missing event and quote-time sources are explicitly distinguished from a refreshable candle gap.

- A manual candles/breadth refresh no longer requires occupying the live-stream lease. Admin authorization and provider limits still apply.
- Socket option observations reach the selected-contract panel independently of the saved chain snapshot. Stop/age/identity checks label retained data; observations never change strict quote eligibility or entered risk assumptions.
- Live subscriptions cover the displayed ATM +/-5 window. A master-verified selected contract is included on connect/reconnect even outside that window.

## External gates still open

- The event calendar has no authorised, verified source connected. `context_fresh` remains false. Market opening or a price refresh cannot fix this.
- Current option-chain/socket depth observations do not expose an independent exchange quote timestamp. Receipt time and last-trade time are not substituted. Strict quote eligibility and IV/Greeks gates remain closed.
- Current-session bars and breadth require an authorised session refresh during market hours. A healthy socket alone does not certify these datasets.
- Historical collection failures remain retained as failures. Returned row counts are not complete coverage certification.
- Exact-contract option execution history, bid/ask fills and forward validation remain unverified; option backtest P&L and accuracy claims stay unavailable.

No credentials, orders, collector plans, or live-stream leases are changed by this release.

## Provider timestamp audit

Official Dhan Live Market Feed documentation lists LTT in ticker, quote and full packets. Full Market Depth documents a sequence number, not an exchange timestamp. These fields are not promoted to a separate bid/ask timestamp.

- https://dhanhq.co/docs/v2/live-market-feed/
- https://dhanhq.co/docs/v2/full-market-depth/

The displayed subscription window and selected live observation bridge have synthetic packet/UI regression tests. Actual market-session delivery is still a rollout verification task.

## Consolidated production audit

Production UI audit observed one HTTP 500 on initial workspace load; a saved-data retry recovered and loaded the chart and chain. Backend root cause is unconfirmed. This release retries a transient workspace GET once and preserves HTTP status for reconnect decisions. It does not automatically retry writes or remove the underlying source failure.

Other confirmed defects fixed: failed selected-contract refresh invalidates earlier setup confirmation; stop/reconnect labels cannot claim an active recent trade; HTTP authentication/validation failures stop reconnect; trend-pullback risk plans preserve their setup in Paper Journal; changing setups invalidates prepared paper plans.

Deployment now reads both actual Trading Room workspace APIs and checks their identity/evidence/setup schema. HTTP 500 or missing setup evidence fails rollout and triggers the existing traffic rollback. Health identity alone is insufficient.

Operational and strategy sign-off remain open: an authorised fresh event source, independently timestamped quote eligibility, market-hour stream/candle/breadth verification, historical failure investigation, exact-contract execution history, and actual forward validation. Unit/UI tests are not a substitute for those checks.

## Research-to-paper workflow — 3.7.0

The shared setup evaluator now emits two explicit outputs. `price_pattern` reports closed-candle structure/alignment/trigger progress. The existing strict `state` still includes event, breadth, quote freshness, liquidity and room-to-level checks. A price trigger is not an entry recommendation and does not upgrade missing quote/event evidence. Current stale candles do not become a current price trigger.

The chart shows observed underlying structure, trigger and invalidation levels for its displayed session; these are never option premium stops. Risk planning takes an explicit premium stop and target and computes target reward net of exit-specific saved cost assumptions. A new hypothetical paper entry requires that target, exact master identity and a sized plan. Stop, target, selected setup and full evidence snapshot are retained.

Closing an assumed paper trade computes net P&L and R from the saved plan. The protected ledger reports clearly labelled hypothetical win rate, net expectancy, average R, profit factor and exit-ordered drawdown. Empty samples stay unavailable and validated-trade counts remain zero. No fake forward trades or automated fills are created.

Underlying replay shares the price-pattern evaluator and only passes closed-bar prefixes. Its pattern view explicitly assumes bar availability at close; actual ingestion timing/revisions and exact-option executions are not simulated. Strict timestamp eligibility is retained independently.

Validation includes a full API integration scenario from risk plan to exact-contract paper entry, close and ledger metrics, plus regressions for the independent price/evidence states, stale-candle rejection, explicit targets, net costs and exit-ordered drawdown. Production deployment and real market-hour delivery still require verification.
