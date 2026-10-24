"""Phase 1 smoke test. Copy into the API container; never prints credentials.

Real data requests only: no orders, deletion, mapping repair or commodities.
"""
import argparse
import json
import sys
import time
from pathlib import Path
import httpx


def show(value):
    print(json.dumps(value, indent=2, default=str), flush=True)


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f'HTTP {response.status_code}: non-JSON API response') from None
    if response.status_code >= 400:
        show({'http_status': response.status_code, 'response': body})
        raise RuntimeError('API request rejected; see response above')
    return body


def watch(client, jid, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        job = request(client, 'GET', f'/history/jobs/{jid}')
        snapshot = {k: job.get(k) for k in ('id', 'status', 'attempts', 'rows_received',
                    'inserted_rows', 'conflicting_rows', 'failure_reason', 'next_attempt_at')}
        if snapshot != previous:
            show(snapshot)
            previous = snapshot
        if job['status'] not in {'QUEUED', 'RUNNING', 'RETRY_WAIT'}:
            attempts = request(client, 'GET', '/history/attempts', params={'jid': jid, 'limit': 100})
            show(attempts)
            passed = job['status'] == 'ROWS_RECEIVED' and job['rows_received'] > 0 and job['conflicting_rows'] == 0
            print(('PASS' if passed else 'REVIEW') + ': historical job ' + jid, flush=True)
            return passed
        if time.monotonic() >= deadline:
            print('PENDING: polling deadline reached; the durable job continues. Inspect its status/logs.', flush=True)
            return False
        time.sleep(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--api-key-file', default='/run/secrets/api_key')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('auth')
    commands.add_parser('summary')
    job = commands.add_parser('job')
    job.add_argument('job_id')
    job.add_argument('--timeout', type=int, default=300)
    fetch = commands.add_parser('fetch')
    fetch.add_argument('--security-id', required=True)
    fetch.add_argument('--symbol', required=True)
    fetch.add_argument('--segment', required=True)
    fetch.add_argument('--instrument-type', required=True)
    fetch.add_argument('--interval', choices=['1d', '1', '5', '15', '25', '60'], default='1d')
    fetch.add_argument('--start', required=True)
    fetch.add_argument('--end', required=True, help='Exclusive end date')
    fetch.add_argument('--timeout', type=int, default=300)
    fetch.add_argument('--confirm-identity', action='store_true', help='Confirm that you checked this exact identity against Dhan')
    args = parser.parse_args(argv)
    if args.command == 'fetch':
        if not args.confirm_identity:
            parser.error('Verify the exact identity first, then pass --confirm-identity')
        if args.segment.startswith('MCX') or args.instrument_type in {'FUTCOM', 'OPTFUT'}:
            parser.error('COMMODITY_HOLD: no commodity tests or mapping changes')
    key = Path(args.api_key_file).read_text().strip()
    if len(key) < 32:
        raise RuntimeError('API key missing/too short; run setup_secrets.py locally')
    with httpx.Client(base_url=args.url, headers={'X-API-Key': key}, timeout=45,
                      follow_redirects=False) as client:
        if args.command == 'auth':
            result = request(client, 'POST', '/auth/check')
            show(result)
            passed = result.get('authenticated') is True
            print('PASS: Dhan profile authentication' if passed else 'REVIEW: Dhan authentication/access')
            return 0 if passed else 2
        if args.command == 'summary':
            show(request(client, 'GET', '/history/error-summary'))
            return 0
        if args.command == 'job':
            return 0 if watch(client, args.job_id, args.timeout) else 2
        identity = dict(security_id=args.security_id, symbol=args.symbol,
                        exchange_segment=args.segment, instrument_type=args.instrument_type,
                        mapping_verified=True)
        registered = request(client, 'POST', '/instruments', json=identity)
        if any(registered.get(k) != identity[k] for k in identity):
            show(registered)
            raise RuntimeError('Existing identity differs or is unverified. No automatic remapping performed.')
        queued = request(client, 'POST', '/history/jobs', json=dict(
            security_id=args.security_id, exchange_segment=args.segment,
            interval=args.interval, start=args.start, end=args.end))
        show(queued)
        # Keep sequential polling explicit: each 90-day chunk is independently checked.
        passed = True
        for jid in queued['job_ids']:
            passed = watch(client, jid, args.timeout) and passed
        print('PASS: all requested chunks returned validated rows' if passed else 'REVIEW: some requested chunks need attention')
        print('Full session coverage and live continuity are not certified by this smoke test.')
        return 0 if passed else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, httpx.HTTPError) as exc:
        # No request headers, authentication tokens or database URLs in output.
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(2)
