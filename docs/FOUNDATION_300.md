# Theta Plus data foundation 3.0.0

Owner selected option 1: remove old analysis screens and logic, retain Admin, Dhan connection and all saved data. No storage delete/migration or secret reset is performed.

Removed from canonical live-app: deterministic engine.py, old trading-room and market-toolbar JS, live toolbar streaming module, chart vendor assets and the retired engine regression test file. Removed runtime calculation paths for overview, trend/option decisions, planning, sizing, experiments and old journal writes. Unknown/retired API routes return 410. No automatic signal refresh/polling remains. Shared identity/timezone moved to runtime.py; Dhan catalog, historical collection, chain collection and field audit remain.

UI has Home, Dhan data, Saved data and Admin. Home explicitly says the new trend model is not implemented and produces no recommendations. Saved Dhan records retain raw JSON export and quality checks. Previously stored datasets, journal, experiments, plans, context, contract metadata and snapshots can be exported through a read-only archive. Existing data schemas remain compatible types, not an active trading engine. No fabricated prices or trend scores.

The existing Admin password, atomic Dhan credential pair, daily hidden-token update, client-ID persistence, test-without-save and check-before-save are retained. Broker execution remains disabled. Existing database/GCS content and credentials are not deleted. Git history and old Cloud Run rollback revisions are retained; this is not a purge of backups or historic repository source.

Validation: 33 current tests pass (not cumulative with removed engine tests). They cover retired API denial, data/journal preservation with exact before/after records, saved Dhan JSON and quality access, no active old navigation, runtime identity and retained admin credential security. JavaScript, Python compilation and release shell syntax pass. Release smoke now checks the foundation shell, exact revision/version, denied anonymous token writes, retired overview 410 and retained data listing. Production deployment and tablet UI verification remain pending.

Deploy from live-app using deploy/release-existing.sh. No password setup or credential re-entry is required for an already configured service. Existing runtime secrets and storage settings are inherited. Review point: archived research is retained for reference and is not revalidated or relabelled as live data.
