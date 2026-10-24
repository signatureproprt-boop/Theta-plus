"""VM release manager: verified main commits only, durable volumes, app rollback."""
import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPO_URL = 'https://github.com/signatureproprt-boop/Theta-plus.git'
API = 'https://api.github.com/repos/signatureproprt-boop/Theta-plus'
ROOT = Path('/srv/personal-trading')
SOURCE = Path('/opt/personal-trading-source')
CHECK = 'Verify real Docker Timescale Redis and Celery integration'


def run(*args, cwd=None, env=None, capture=False):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip() if capture else subprocess.run(args, cwd=cwd, env=env, check=True)


def get(path):
    with urlopen(Request(API + path, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'personal-trading-deployer'}), timeout=30) as r:
        return json.load(r)


def verified(sha):
    runs = get('/actions/runs?head_sha=' + sha + '&per_page=30')['workflow_runs']
    for item in runs:
        if item['path'] != '.github/workflows/personal-trading-phase1.yml' or item['head_branch'] != 'main' or item['event'] not in ('push', 'workflow_dispatch'):
            continue
        jobs = get('/actions/runs/' + str(item['id']) + '/jobs?per_page=100')['jobs']
        return any(j['name'] == CHECK and j['conclusion'] == 'success' for j in jobs)
    return False


def compose(release, sha, *args, capture=False):
    env = {**os.environ, 'PHASE1_APP_IMAGE': 'personal-trading-phase1:' + sha}
    return run('docker', 'compose', '-p', 'personal-trading-phase1', '-f', 'compose.yaml',
               '-f', 'compose.vps.yaml', *args, cwd=release, env=env, capture=capture)


def healthy(release, sha):
    image = run('docker', 'image', 'inspect', 'personal-trading-phase1:' + sha, '--format={{.Id}}', capture=True)
    for _ in range(60):
        try:
            for service in ('api', 'worker', 'dispatcher', 'live'):
                cid = compose(release, sha, 'ps', '-q', service, capture=True)
                if not cid or run('docker', 'inspect', cid, '--format={{.State.Running}} {{.Image}}', capture=True) != 'true ' + image:
                    raise RuntimeError('Release container not active')
            compose(release, sha, 'exec', '-T', 'api', 'python', '-c',
                    "import httpx; r=httpx.get('http://127.0.0.1:8000/health/ready',timeout=10);r.raise_for_status()")
            compose(release, sha, 'exec', '-T', 'worker', 'celery', '-A', 'app.celery_app:celery_app', 'inspect', 'ping', '--timeout=10')
            return True
        except (subprocess.CalledProcessError, RuntimeError):
            time.sleep(5)
    return False


def main(revision=None):
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the dedicated Phase 1 VM')
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'deploy.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('UPDATE_ALREADY_RUNNING');return
        if not SOURCE.exists():
            run('git', 'clone', '--no-checkout', '--filter=blob:none', REPO_URL, str(SOURCE))
        run('git', 'fetch', 'origin', 'main', cwd=SOURCE)
        candidate = revision or run('git', 'log', '-1', '--format=%H', 'origin/main', '--',
                                   'personal-trading-phase1', 'live-app', '.github/workflows/personal-trading-phase1.yml', cwd=SOURCE, capture=True)
        if not re.fullmatch('[a-f0-9]{40}', candidate):
            raise RuntimeError('Invalid release revision')
        run('git', 'merge-base', '--is-ancestor', candidate, 'origin/main', cwd=SOURCE)
        current = ROOT / 'current'
        previous = current.resolve() if current.exists() else None
        state_path = ROOT / 'release.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get('sha') == candidate:
            print('ALREADY_DEPLOYED', candidate);return
        if not verified(candidate):
            print('WAITING_FOR_SUCCESSFUL_DOCKER_INTEGRATION', candidate);return
        if shutil.disk_usage(ROOT).free < 5 * 1024 ** 3:
            raise RuntimeError('Less than 5 GiB free; release deferred without deleting data')
        bundle = ROOT / 'releases' / candidate
        release = bundle / 'personal-trading-phase1'
        if not release.exists():
            bundle.mkdir(parents=True, exist_ok=True)
            archive = subprocess.Popen(['git', 'archive', candidate, 'personal-trading-phase1', 'live-app'], cwd=SOURCE, stdout=subprocess.PIPE)
            subprocess.run(['tar', '-x', '-C', str(bundle)], stdin=archive.stdout, check=True)
            archive.stdout.close()
            if archive.wait():
                raise RuntimeError('Source extraction failed')
        secret_dir = ROOT / 'secrets'
        secret_dir.mkdir(mode=0o700, exist_ok=True)
        secret_dir.chmod(0o700)
        for name in ('postgres_password', 'api_key', 'dhan_client_id', 'dhan_access_token', 'legacy_admin_hash'):
            path = secret_dir / (name + '.txt')
            if not path.exists():
                path.write_text(secrets.token_urlsafe(48) if name in ('postgres_password', 'api_key') else '')
            path.chmod(0o644)
            if name in ('dhan_client_id', 'dhan_access_token'):
                os.chown(path, 10001, 10001)
        env_path = ROOT / '.env'
        if not env_path.exists():
            shutil.copyfile(release / '.env.example', env_path)
            env_path.chmod(0o600)
        for name, target in [('secrets', secret_dir), ('.env', env_path)]:
            link = release / name
            if not link.exists():
                link.symlink_to(target)
        incoming = ROOT / 'import'
        incoming.mkdir(mode=0o700, exist_ok=True)
        os.chown(incoming, 10001, 10001)
        compose(release, candidate, 'config', '--quiet')
        compose(release, candidate, 'build', 'api')
        if previous and state.get('sha'):
            backup_dir = ROOT / 'backups'
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup = backup_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.dump')
            env = {**os.environ, 'PHASE1_APP_IMAGE': 'personal-trading-phase1:' + state['sha']}
            with backup.open('xb') as output:
                subprocess.run(['docker', 'compose', '-p', 'personal-trading-phase1', '-f', 'compose.yaml', '-f', 'compose.vps.yaml',
                                'exec', '-T', 'postgres', 'pg_dump', '-U', 'trading', '-d', 'trading', '-Fc'],
                               cwd=previous, env=env, stdout=output, check=True)
            if not backup.stat().st_size:
                raise RuntimeError('Empty backup; deployment stopped')
        try:
            compose(release, candidate, 'up', '-d', '--no-build')
            if not healthy(release, candidate):
                raise RuntimeError('Candidate readiness failed')
        except Exception:
            if previous and state.get('sha'):
                compose(previous, state['sha'], 'up', '-d', '--no-build')
                if not healthy(previous, state['sha']):
                    raise RuntimeError('Rollback requires operator review') from None
                print('PREVIOUS_APPLICATION_RELEASE_RESTORED; database was not rolled back')
            raise
        pending = ROOT / 'current.next'
        if pending.is_symlink():
            pending.unlink()
        pending.symlink_to(release)
        pending.replace(current)
        state_path.write_text(json.dumps({'sha': candidate, 'deployed_at': datetime.now(timezone.utc).isoformat(),
                                         'state': 'STACK_READY', 'dhan_credentials_configured': bool((secret_dir / 'dhan_access_token.txt').read_text().strip())}))
        shutil.copyfile(release / 'deploy/auto_update.py', '/usr/local/bin/personal-trading-update.py')
        print('PHASE1_STACK_DEPLOYED', candidate, 'API_LOOPBACK_ONLY; DHAN_AUTH_REQUIRES_SEPARATE_CHECK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision')
    main(parser.parse_args().revision)
