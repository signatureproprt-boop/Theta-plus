"""Read-only archive worker on the persistent VM. No owner login or key files."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import time
from urllib.request import (Request, build_opener, ProxyHandler,
                            HTTPRedirectHandler)

from archive_cloudshell import Storage, mirror, file_sha

STATE = Path('/srv/personal-trading/archive-worker')
SERVICE = 'theta-archive-copy.service'
METADATA = 'http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class VMStorage(Storage):
    def __init__(self):
        super().__init__(None)
        self.expires_at = 0
        # Never send metadata credentials through a configured HTTP proxy.
        self.metadata = build_opener(ProxyHandler({}), NoRedirect())

    def access_token(self, force=False):
        with self.lock:
            if force or self.token is None or time.monotonic() >= self.expires_at:
                request = Request(METADATA, headers={'Metadata-Flavor': 'Google'})
                try:
                    with self.metadata.open(request, timeout=10) as response:
                        if response.headers.get('Metadata-Flavor') != 'Google':
                            raise ValueError('UNEXPECTED_METADATA_SERVER')
                        raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError('METADATA_RESPONSE_TOO_LARGE')
                    data = json.loads(raw)
                    token, expires = data['access_token'], int(data['expires_in'])
                    if not isinstance(token, str) or not token or expires < 60:
                        raise ValueError('INVALID_METADATA_TOKEN')
                except Exception:
                    # Never log response bodies, tokens or credential-bearing errors.
                    raise RuntimeError('VM_METADATA_AUTH_UNAVAILABLE') from None
                self.token = token
                self.expires_at = time.monotonic() + min(expires - 30, 2700)
            return self.token


def write_json(path, value):
    temporary = path.with_suffix('.new')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path):
    if path.is_symlink():
        raise RuntimeError('UNSAFE_WORKER_STATE')
    return json.loads(path.read_text()) if path.exists() else {}


def request_id(value):
    if not re.fullmatch('[a-f0-9]{32}', value):
        raise RuntimeError('INVALID_ARCHIVE_REQUEST')
    return value


@contextmanager
def locked(folder):
    with (folder/'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('VM_ARCHIVE_BUSY') from None
        yield


def status(folder, expected):
    value = read_json(folder/'status.json')
    if value.get('request') != expected:
        return {'state': 'PENDING', 'request': expected}
    if value.get('state') == 'VERIFIED':
        manifest = folder/'archive/migration-manifest.json'
        if not manifest.is_file() or file_sha(manifest) != value.get('manifest_sha256'):
            raise RuntimeError('VM_ARCHIVE_MANIFEST_CHANGED')
    return value


def queue(folder, expected):
    with locked(folder):
        write_json(folder/'request.json', {'request': request_id(expected)})
        write_json(folder/'status.json', {'request': expected, 'state': 'PENDING'})


def execute(folder, storage=None):
    with locked(folder):
        expected = request_id(read_json(folder/'request.json')['request'])
        if status(folder, expected).get('state') == 'VERIFIED':
            print('VM_ARCHIVE_ALREADY_VERIFIED', expected, flush=True)
            return
        write_json(folder/'status.json', {'request': expected, 'state': 'COPYING'})
        try:
            archive = mirror(storage or VMStorage(), folder)
            write_json(folder/'status.json', {'request': expected, 'state': 'VERIFIED',
                'manifest_sha256': file_sha(archive/'migration-manifest.json')})
            print('VM_ARCHIVE_COPY_COMPLETE', expected, flush=True)
        except Exception:
            write_json(folder/'status.json', {'request': expected, 'state': 'RETRYING'})
            print('VM_ARCHIVE_COPY_RETRY; checkpoints retained; inspect journal', flush=True)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'queue', 'status', 'probe'])
    parser.add_argument('request', nargs='?')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'probe':
        next(VMStorage().pages())
        print('VM_BUCKET_READ_OK')
    elif args.action == 'run':
        execute(STATE)
    else:
        expected = request_id(args.request or '')
        if args.action == 'queue':
            queue(STATE, expected)
        else:
            print(json.dumps(status(STATE, expected)))


if __name__ == '__main__':
    main()
