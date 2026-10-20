"""Bounded, read-only rollout verification; no credentials required."""
import json
import sys
import time
import urllib.request


def fetch_health(url, timeout):
    with urllib.request.urlopen(url.rstrip('/') + '/health-check', timeout=timeout) as response:
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


def fetch_workspace(url, symbol, timeout):
    with urllib.request.urlopen(url.rstrip('/')+'/api/research/workspace?symbol='+symbol,timeout=timeout) as response:
        return json.load(response)


def verify_workspaces(url, fetch=fetch_workspace):
    # Health alone cannot detect a broken chart/chain workspace.
    for symbol in ('nifty','banknifty'):
        data=fetch(url,symbol,120)
        if not isinstance(data,dict) or data.get('symbol')!=symbol or data.get('mode')!='READ_ONLY_RESEARCH':
            raise RuntimeError('Trading Room workspace identity/schema invalid: '+symbol)
        if not isinstance(data.get('history',{}).get('candles'),list) or not isinstance(data.get('chain',{}).get('contracts'),list):
            raise RuntimeError('Trading Room evidence schema invalid: '+symbol)
        if {s.get('setup') for s in data.get('setups',[])}!={'opening_range','trend_pullback'}:
            raise RuntimeError('Trading Room setup checks missing: '+symbol)
    return True


if __name__ == '__main__':
    if sys.argv[1]=='--workspaces':
        verify_workspaces(sys.argv[2]);print('Both Trading Room workspaces verified; research eligibility remains separate.')
    else:
        wait_for_health(*sys.argv[1:]);print('Exact candidate health verified.')
