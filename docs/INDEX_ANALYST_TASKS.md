# Index Analyst integration tracker

Scope: integrate into Theta Plus; NIFTY first, pullback-continuation research, no automatic execution changes.

## Inspected source

Repository main e62801964ef5068c9db0a83df2914a612fe65e5e contains only Theta-pluse-main.zip. The archive is dated 2026-09-16. It is not evidence of the currently deployed source or later research-lake versions.

## Task status

1. DONE: locate repository and inspect archived code inventory.
2. NEXT: establish canonical current source; compare archive with running version before integration.
3. PENDING: audit live/historical feed coverage and timestamp semantics.
4. PENDING: freeze measurable rulebook, timeframe conflicts, transitions and risk envelope.
5. PENDING: freeze execution costs, baselines, sample planning, holdout and stop criteria.
6. PENDING: extend causal replay and append-only decision provenance.
7. PENDING: evaluate trend engine against baselines and ablations.
8. PENDING: options feasibility, contract selection and one setup evaluation.
9. PENDING: forward paper evaluation and failure tests.
10. PENDING: grounded AI explanation and dashboard integration.

## Verified inventory (file inspection, not runtime validation)

- backend/data/dhan_client.py: async Dhan market-data connector, environment credentials, endpoint throttling.
- backend/engines/pcr_engine.py: full-chain and ATM-range OI PCR, missing/zero denominator handling, historical PCR change/trend. Multi-expiry aggregation is not established by this inspection.
- backend/engines/: price, OI, VWAP, feature, score, rule and state-machine modules exist; integration suitability remains unverified.
- backend/storage/snapshot_store.py: SQLite snapshot store with duplicate-ID first-write-wins behavior, raw/derived separation and strategy/feature versions. Database-level tamper resistance and exchange/received-time provenance are not established.
- backend/routers/replay_router.py: synthetic/SQLite replay API; synthetic output is explicitly distinguished from historical output.
- backend/research/: quality, validation, OOS and parity modules exist; methodology remains unverified.
- backend/paper/ and backend/execution/: paper and execution modules already exist. Do not activate or change live order routing for this research integration.
- backend/tests/: existing data, rules, replay, paper and integration tests exist. No tests run yet in this inventory task.

## Immediate integration gate

Confirm whether the September archive is canonical or a newer source exists. Do not overwrite a running system from an older archive. No claim of live feed verification, passing tests or deployment has been made.
