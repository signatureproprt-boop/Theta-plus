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


def ready():
    global _cache
    if _cache[0] + 30 > time.monotonic():
        return _cache[1]
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(URL)
        response.raise_for_status()
        valid = approved(response.json())
    except (httpx.HTTPError, ValueError, TypeError):
        valid = False
    _cache = (time.monotonic(), valid)
    return valid
