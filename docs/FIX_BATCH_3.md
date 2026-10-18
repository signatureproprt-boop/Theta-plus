# Batch 3: execution journal and restart recovery

Depends on PR #3 (and PR #2). No deployment, live arming or broker orders.

Changes:
- SQLite append-only state events commit order intent/idempotency reservation and risk counters before broker submission. Status/fill updates, reported realised P&L and kill-switch changes are journaled.
- Restart restores records, exposure, counters, daily loss lock and kill switch. Reconciliation starts PENDING. Unresolved/unknown orders are queried by broker/correlation ID, never blindly resent.
- Reconciliation checks contract ID, exchange segment, product and net quantity, not just position count. Missing/unresolved information blocks entry.
- Filled quantity is monotonic. Terminal acknowledgements without authoritative status do not release exposure reservations.
- EXECUTION_JOURNAL_PATH configures a disk-backed journal. Actual Dhan execution cannot be enabled with an in-memory journal. The path must be on a persistent mounted filesystem for deployment durability; no mount/configuration was changed or verified here.
- A nonblocking process file lock prevents two writers to the same journal path. Unsupported/corrupt storage fails rather than resetting. Broker/account namespace is bound by a hash; credentials are not stored. Use one journal per account.
- Journal write failure engages kill switch and marks reconciliation pending; intent-save failure sends zero broker submissions.
- Daily trade/P&L limits reset on a new IST decision date; existing order exposure/idempotency and kill switch persist. This is not an exchange holiday calendar or realised-P&L ingestion implementation.

Validation: 388 backend tests passed, 1 skipped, with local API server, dummy Mongo configuration, synthetic fixtures and mock broker. Thirteen new regressions cover restart, dedupe, risk/kill restoration, disk failure, uncertain orders, wrong security/quantity/product, single-writer/corrupt storage, day rollover, in-memory real-adapter block and account isolation. No browser changes; no new frontend build required.

Remaining blockers: automatic protective exits, confirmed close accounting/P&L ingestion, continuous broker reconciliation, shared live/replay futures storage/parity and real provider/deployment verification. Filled slots stay conservatively reserved until an explicit confirmed exit lifecycle exists. Separate journal paths across workers do not provide distributed coordination. This implementation requires single-account/single-writer deployment discipline. Journal retention/compaction and full production restore drills remain operations tasks. LIVE defaults remain off; this is not production trading approval.
