# Audit fix batch 2

Depends on PR #2. No deployment or live activation.

- Entry gate/submission serialized with an asyncio lock within one service instance. REQUESTED/PENDING/UNKNOWN reserve exposure slots before broker await. Confirmed fills retain a slot, including cancelled remainders; unfilled rejection/cancellation releases its reservation. Fill quantity cannot decrease on a later status response.
- Zero/nonfinite/negative-volume VWAP no longer falls back to an unweighted mean.
- Dhan live feed uses closed, contiguous timestamped one-minute spot candles for price structure, and separately mapped active NIFTY FUTIDX candles for VWAP. No quote-day-OHLC/cumulative-volume synthesis. Spot and futures price bases remain separate; VWAP comparison uses last completed futures close.
- Futures mapping comes from DHAN_INSTRUMENT_MAP_PATH. Missing mapping/candles/volume blocks VWAP rather than guessing a security ID or substituting spot volume. Duplicate, gap, off-grid and malformed OHLCV series are rejected. Completed-session data is refreshed once per minute; provider delays can therefore produce WAIT until complete candles arrive.
- Dashboard labels Futures VWAP and Futures close minus VWAP when this source is active. Evidence uses reference price rather than claiming spot was compared with futures VWAP.
- Validation: 375 backend tests passed, 1 skipped; frontend build/lint passed. Added 17 exposure/candle/request-contract regressions, including concurrent entries and missing futures mapping. Tests use mocks/synthetic fixtures and a local API server; no live provider verification or real broker calls.

Known limits: per-process locking/reservations are not durable or multi-worker-safe. Filled slots remain conservatively occupied until the still-pending confirmed-exit lifecycle is implemented. Live execution must remain disabled. Actual broker reconciliation/restart recovery, protective exits, received-time freshness, and shared live/replay futures-candle storage/parity remain audit tasks. Existing replay inputs have not been migrated into a futures-candle schema. This change is not a performance/production approval.

Dhan intraday request contract checked against https://dhanhq.co/docs/v2/historical-data/ .
