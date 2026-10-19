# Trading Room readiness — 3.6.1

The room is research software, not a validated entry recommendation.

## Fixed

- Current-session refresh passes the selected security ID into the shared setup evaluator.
- A response started before a contract/symbol/workspace change cannot replace newer setup evidence.
- Session refresh reuses the returned workspace instead of making a redundant full workspace request.
- Setup panel groups missing data, blocked rules, pending triggers and passed conditions. Missing event and quote-time sources are explicitly distinguished from a refreshable candle gap.

## External gates still open

- The event calendar has no authorised, verified source connected. `context_fresh` remains false. Market opening or a price refresh cannot fix this.
- Current option-chain/socket depth observations do not expose an independent exchange quote timestamp. Receipt time and last-trade time are not substituted. Strict quote eligibility and IV/Greeks gates remain closed.
- Current-session bars and breadth require an authorised session refresh during market hours. A healthy socket alone does not certify these datasets.
- Historical collection failures remain retained as failures. Returned row counts are not complete coverage certification.
- Exact-contract option execution history, bid/ask fills and forward validation remain unverified; option backtest P&L and accuracy claims stay unavailable.

No credentials, orders, collector plans, or live-stream leases are changed by this release.
