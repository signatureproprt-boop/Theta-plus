"""Owner command: seed cached files and run read-only copying on the existing VM.

Creates one bucket-only reader identity and attaches it to the VM (one restart).
Does not run the handover, fence collectors, or start a broker connection.
"""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile
import time
import uuid
from urllib.request import urlopen

from archive_cloudshell import workspace, file_sha
from auto_update import verified
from merge_cloudshell import (cloud, ssh, remote, run, current_fence, PROJECT,
                              ZONE, VM, ROOT, api_module)

ACCOUNT = 'theta-archive-reader@' + PROJECT + '.iam.gserviceaccount.com'
TITLE = 'Theta archive bucket reader v1'
BUCKET = 'gs://theta-pluse-theta-plus-data'
REMOTE_STATE = '/srv/personal-trading/archive-worker'
WORKER = '/opt/theta-archive-worker/archive_vm.py'
SERVICE = 'theta-archive-copy.service'
SCOPE = 'https://www.googleapis.com/auth/devstorage.read_only'

WORKER_USER_SETUP = '''
import grp, pwd, subprocess
name = 'thetaarchive'
uid = gid = 10001
def lookup(function, value):
    try: return function(value)
    except KeyError: return None
group = lookup(grp.getgrgid, gid)
user = lookup(pwd.getpwuid, uid)
named_group = lookup(grp.getgrnam, name)
named_user = lookup(pwd.getpwnam, name)
if named_group and named_group.gr_gid != gid:
    raise SystemExit('ARCHIVE_GROUP_NAME_CONFLICT; existing identity retained')
if named_user and named_user.pw_uid != uid:
    raise SystemExit('ARCHIVE_USER_NAME_CONFLICT; existing identity retained')
if group is None:
    subprocess.run(['groupadd', '--system', '--gid', str(gid), name], check=True)
if user is None:
    subprocess.run(['useradd', '--system', '--uid', str(uid), '--gid', str(gid),
        '--no-create-home', '--home-dir', '/srv/personal-trading/archive-worker',
        '--shell', '/usr/sbin/nologin', name], check=True)
assert pwd.getpwuid(uid).pw_uid == uid and grp.getgrgid(gid).gr_gid == gid
print('VM_ARCHIVE_USER_READY')
'''


def ensure_worker_user():
    remote('sudo', 'python3', '-c', WORKER_USER_SETUP)


def start_worker():
    ssh('sudo systemctl enable '+SERVICE+' && sudo systemctl start --no-block '+SERVICE)
    expected = json.loads(remote('sudo','cat',REMOTE_STATE+'/request.json',capture=True))['request']
    for _ in range(20):
        receipt = json.loads(remote('sudo','python3',WORKER,'status',expected,capture=True))
        if receipt.get('state') == 'VERIFIED':
            print('VM_ARCHIVE_COPY_COMPLETE; import/handover still pending', flush=True)
            return
        pid = remote('sudo','systemctl','show',SERVICE,'--property=MainPID','--value',capture=True)
        if receipt.get('state') == 'COPYING' and str(pid).strip().isdigit() and int(pid) > 0:
            print('VM_ARCHIVE_RUNNING; worker process confirmed; import/handover still pending', flush=True)
            return
        time.sleep(1)
    raise RuntimeError('VM_ARCHIVE_NOT_STARTED; inspect journal; cache retained')


def preflight():
    marker, _ = current_fence()
    with urlopen('https://theta-plus-jw4jpkebxa-el.a.run.app/api/migration/status', timeout=30) as response:
        public = json.load(response)
    if marker.get('state') in ('NEW_BACKEND', 'FENCED') or public.get('state') != 'LEGACY_BACKEND':
        raise RuntimeError('HANDOVER_STATE_REQUIRES_REVIEW; no VM restart')
    # Refuse a live VM owner even if a stale public marker still says legacy.
    state = json.loads(api_module('app.verify_phase1', 'live-status', capture=True))
    if state.get('owner', {}).get('state') not in ('HOLD', 'AWAITING_VERIFIED_HANDOVER'):
        raise RuntimeError('VM_LIVE_OWNER_REQUIRES_REVIEW; no VM restart')


def validate_accounts(instance):
    accounts = instance.get('serviceAccounts', [])
    if accounts and (len(accounts) != 1 or accounts[0].get('email') != ACCOUNT or
                     set(accounts[0].get('scopes', [])) != {SCOPE}):
        raise RuntimeError('EXISTING_VM_IDENTITY_REQUIRES_REVIEW')
    return bool(accounts)


def configure_identity():
    instance = json.loads(cloud('compute', 'instances', 'describe', VM, '--zone='+ZONE, '--format=json', capture=True))
    attached = validate_accounts(instance)
    # List distinguishes absence from a describe permission error.
    accounts = json.loads(cloud('iam', 'service-accounts', 'list', '--format=json', capture=True))
    account = next((a for a in accounts if a.get('email') == ACCOUNT), None)
    if account is not None and (account.get('displayName') != TITLE or account.get('disabled')):
        raise RuntimeError('EXISTING_READER_IDENTITY_REQUIRES_REVIEW')
    policy = json.loads(cloud('projects', 'get-iam-policy', PROJECT, '--format=json', capture=True))
    member = 'serviceAccount:' + ACCOUNT
    if any(member in b.get('members', []) for b in policy.get('bindings', [])):
        raise RuntimeError('READER_HAS_PROJECT_ROLES; refusing broader identity')
    if account is None:
        cloud('iam', 'service-accounts', 'create', 'theta-archive-reader', '--display-name='+TITLE, capture=True)
    cloud('storage', 'buckets', 'add-iam-policy-binding', BUCKET,
          '--member='+member, '--role=roles/storage.objectViewer', '--condition=None', capture=True)
    if not attached:
        # Recheck immediately before the only disruptive operation.
        preflight()
        print('VM_RESTART_FOR_BUCKET_READER; persistent disks retained', flush=True)
        cloud('compute', 'instances', 'stop', VM, '--zone='+ZONE, '--quiet')
        try:
            cloud('compute', 'instances', 'set-service-account', VM, '--zone='+ZONE,
                  '--service-account='+ACCOUNT, '--scopes=storage-ro', '--quiet')
        finally:
            # Also bring the existing app back if setting the identity fails.
            cloud('compute', 'instances', 'start', VM, '--zone='+ZONE, '--quiet')
        for _ in range(30):
            try:
                ssh('true', capture=True)
                break
            except subprocess.CalledProcessError:
                time.sleep(5)
        else:
            raise RuntimeError('VM_SSH_NOT_READY; no downloads discarded')


def seed_cache(folder):
    checkpoint = folder/'archive-checkpoints.sqlite'
    archive = folder/'archive'
    if not checkpoint.is_file() or not archive.is_dir():
        print('NO_LOCAL_CACHE; worker will download on persistent VM', flush=True)
        return
    if checkpoint.is_symlink() or archive.is_symlink():
        raise RuntimeError('UNSAFE_LOCAL_CACHE')
    # Called under the Cloud Shell workspace lock: no local download can race.
    with tempfile.TemporaryDirectory(prefix='theta-archive-seed-') as temporary:
        os.chmod(temporary, 0o700)
        snapshot = Path(temporary)/checkpoint.name
        with sqlite3.connect('file:'+str(checkpoint)+'?mode=ro', uri=True) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
        snapshot.chmod(0o600)
        # Reject symlinks before a privileged tar extraction on the VM.
        for directory, directories, files in os.walk(archive, followlinks=False):
            if any((Path(directory)/name).is_symlink() for name in directories + files):
                raise RuntimeError('SYMLINK_IN_SEED_ARCHIVE')
        # Stage one regular file instead of piping tar through the gcloud/IAP
        # wrapper's stdin. A closed receiver must not discard the cache transfer.
        package = Path(temporary)/'theta-archive-seed.tar.gz'
        print('VM_CACHE_SEED_PACKING; source cache retained', flush=True)
        with package.open('xb') as output:
            package.chmod(0o600)
            subprocess.run(['tar','-czf','-','-C',temporary,checkpoint.name,
                            '-C',str(folder),'archive'], stdout=output, check=True)
        print('VM_CACHE_SEED_UPLOAD', package.stat().st_size, 'compressed bytes', flush=True)
        cloud('compute','scp',str(package),VM+':~/theta-archive-seed.tar.gz',
              '--zone='+ZONE,'--tunnel-through-iap')
        uploaded = ssh('sha256sum ~/theta-archive-seed.tar.gz', capture=True).split()
        if not uploaded or uploaded[0] != file_sha(package):
            raise RuntimeError('VM_CACHE_SEED_CHECKSUM_MISMATCH; local cache retained')
        home = ssh('printf %s "$HOME"', capture=True)
        seed = shlex.quote(home+'/theta-archive-seed.tar.gz')
        print('VM_CACHE_SEED_EXTRACTING; upload checksum verified', flush=True)
        try:
            remote('sudo','flock','-n','-E','75',REMOTE_STATE+'/run.lock','sh','-c',
                   'tar --no-same-owner --no-same-permissions -xzf '+seed+' -C '+REMOTE_STATE+
                   ' && chown -R 10001:10001 '+REMOTE_STATE+
                   ' && chmod -R u=rwX,go= '+REMOTE_STATE)
        except subprocess.CalledProcessError as error:
            raise RuntimeError('VM_CACHE_SEED_EXTRACT_FAILED; exit='+str(error.returncode)+
                               '; local cache retained; do not delete checkpoints') from None
        # Delete only the temporary uploaded package, after successful extraction.
        ssh('rm -- '+seed)
        print('VM_CACHE_SEED_COMPLETE; cached objects will be SHA-verified before reuse', flush=True)


def install(folder):
    existing = ssh('if sudo test -f '+REMOTE_STATE+'/installed.json; then sudo cat '+REMOTE_STATE+'/installed.json; else echo "{}"; fi', capture=True)
    if json.loads(existing).get('version') == 1:
        ensure_worker_user()
        start_worker()
        print('VM_ARCHIVE_ALREADY_INSTALLED; cache and current request retained', flush=True)
        ssh('sudo systemctl status '+SERVICE+' --no-pager --lines=5 || true')
        return
    preflight()
    configure_identity()
    ensure_worker_user()
    ssh('sudo install -d -m 0755 /opt/theta-archive-worker && '
        'sudo install -d -m 0700 -o 10001 -g 10001 '+REMOTE_STATE)
    deploy = ROOT/'personal-trading-phase1/deploy'
    # Send only code; no owner refresh tokens, service account keys or Dhan secrets.
    cloud('compute','scp',*(str(deploy/name) for name in
        ('archive_vm.py','archive_cloudshell.py','theta-archive-copy.service')),
        VM+':~/', '--zone='+ZONE,'--tunnel-through-iap',capture=True)
    ssh('sudo install -m 0644 ~/archive_vm.py ~/archive_cloudshell.py /opt/theta-archive-worker/ && '
        'sudo install -m 0644 ~/theta-archive-copy.service /etc/systemd/system/ && sudo systemctl daemon-reload')
    # Test metadata auth and a real GCS list before seeding or enabling the service.
    remote('sudo','python3',WORKER,'probe')
    seed_cache(folder)
    expected = uuid.uuid4().hex
    remote('sudo','python3',WORKER,'queue',expected)
    remote('sudo','chown','-R','10001:10001',REMOTE_STATE)
    # Commit the installation marker before starting: a lost SSH reply must not
    # seed over a service that has already begun copying.
    remote('sudo','python3','-c',
        "import json,pathlib; p=pathlib.Path('"+REMOTE_STATE+"/installed.json');"
        "p.write_text(json.dumps({'version':1,'request':'"+expected+"'}));p.chmod(0o600)")
    start_worker()
    (folder/'vm-archive-enabled.json').write_text(json.dumps({'version':1}))
    print('Progress: gcloud compute ssh '+VM+' --project='+PROJECT+' --zone='+ZONE+
          ' --tunnel-through-iap --command="sudo journalctl -u '+SERVICE+' -n 8 --no-pager"')


def main():
    sha = run('git','rev-parse','HEAD',capture=True)
    if not verified(sha):
        raise RuntimeError('WAIT_FOR_SUCCESSFUL_INTEGRATION; no cloud changes made')
    with workspace() as folder:
        install(folder)


if __name__ == '__main__':
    main()
