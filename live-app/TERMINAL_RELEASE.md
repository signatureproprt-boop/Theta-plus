# Compact terminal and data bridge — 3.8.0

The approved reference uses a 40px navigation row, narrow index strip, 23% left
market watch, compact tables/forms, a chart-first workspace and a narrow right
inspector. The existing Cloud Run app now implements that light, compact shell.
Watch prices are retained Dhan observations with explicit timestamps/statuses;
foreign feeds and unconnected account balances remain unavailable.

## Evidence acquired on 21 October 2026

- Priority index job `6cab6783923848429c24c89ac3b04a49`: NIFTY ID13 and
  BankNIFTY ID25, IDX_I, interval1, 30 September–21 October exclusive.
  Both tasks completed, 4,875 rows each, no task failures.
- Raw JSON read independently for each index: 4,875 unique timestamps, all
  regular-session rows. Thirteen returned session dates, each with 375 unique
  minutes and no missing minutes inside those returned sessions. An authorised
  holiday/special-session calendar is still required to certify expected dates.
- TUNWAL and MODEFENCE long requests had failed with DH-905. Their original
  identities returned five daily rows each in a bounded diagnostic window.
  Separate job `e492c14301af47f1ba7f1d3245aa196c` saved those ten rows for
  12–19 October exclusive. Original full-range failures remain unresolved.
- The broad original collection job remained running; no checkpoint reset.

## Existing-data integration

Research reads exact index minute partitions using deterministic series keys,
up to 21 recent calendar days for pre-index archives. New collection creates a
bounded 90-day read index; daily/rolling/other-identity data cannot enter the
minute chart. Future receipts/partition writes are excluded at replay time.
New index chain snapshots populate a nearest-current-expiry read index, using
receipt time only. Existing pre-index chain archives are not retroactively
indexed by this release. A current-expiry refresh remains available in the UI.

A provider-confirmed diagnostic can prepare a separate bounded retry. Only the
verified range is copied; the parent job, failed outcome and original gap remain.
Repair endpoints require the existing Admin authorization.

## Deployment and limits

Use the existing `deploy/release-collector.sh` from authenticated Cloud Shell.
It retains both workspace checks, protected API checks, rollback, immutable image
sync and checkpoint-safe worker handover. No IAM, secrets or account changes.

Local Python/UI checks include archive identity/time isolation, bounded repair,
existing partition loading, watch escaping, no implicit provider POST, and
explicit-only refresh. Offline visual inspection through this cloud browser was
blocked by its local-file URL policy; production visual verification is pending.
All-market history failures, external feeds, exact-option execution replay,
calendar certification and trade validation are not declared complete.
