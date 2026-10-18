"""Bounded, read-only rollout verification; no credentials required."""
import json
import sys
import time
import urllib.request


def fetch_health(url, timeout):
    with urllib.request.urlopen(url.rstrip('/') + '/healthz', timeout=timeout) as response:
        return json.load(response)


def wait_for_health(url, version, revision, timeout=120, fetch=fetch_health,
                    clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    last = 'no response'
    while clock() < deadline:
        try:
            h = fetch(url, min(10, max(0.1, deadline - clock())))
            if (h.get('status') == 'ok' and h.get('version') == version
                    and h.get('revision') == revision
                    and h.get('execution') == 'NO_BROKER_EXECUTION'):
                return h
            last = 'health identity does not match candidate'
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            last = type(exc).__name__ + ': ' + str(exc)
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(2, remaining))
    raise RuntimeError('Rollout verification timed out: ' + last)


if __name__ == '__main__':
    wait_for_health(*sys.argv[1:])
    print('Exact candidate health verified.')
