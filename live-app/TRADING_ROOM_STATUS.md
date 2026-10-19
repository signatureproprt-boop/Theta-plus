# Trading Room readiness — 3.6.2

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
