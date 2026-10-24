"""Reserve capacity for the existing Theta Plus collector; fail closed if unknown."""
import time
import httpx

URL = 'https://theta-plus-jw4jpkebxa-el.a.run.app/api/collection/account-budget'
_cache = (0, False)


def approved(data):
    return (data.get('coordination') == 'parallel-collectors-v1' and
            data.get('legacy_daily_guard') == 50000 and
            data.get('legacy_global_spacing_seconds', 0) >= 0.5 and
            data.get('worker_release_matches') is True)


def ownership_approved(owner, migration):
    digest = migration.get('manifest_sha256')
    return (owner.get('state') == 'NEW_BACKEND' and migration.get('state') == 'VERIFIED' and
            isinstance(digest, str) and len(digest) == 64 and owner.get('archive_manifest_sha256') == digest)


def local_owner_ready():
    from app.db import session
    from app.models import SystemState
    with session() as db:
        owner = db.get(SystemState, 'collection-owner')
        archive = db.get(SystemState, 'legacy-migration')
        return ownership_approved(owner.data if owner else {}, archive.data if archive else {})


def ready():
    global _cache
    if _cache[0] + 30 > time.monotonic():
        return _cache[1]
    try:
        # The verified local handover receipt retires the source-service dependency.
        # Keep conservative 40k accounting in RateGate for the transition's quota day.
        if local_owner_ready():
            valid = True
        else:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = client.get(URL)
            response.raise_for_status()
            valid = approved(response.json())
    except Exception:
        # DB/network failure must fail closed; never log connection credentials.
        valid = False
    _cache = (time.monotonic(), valid)
    return valid
