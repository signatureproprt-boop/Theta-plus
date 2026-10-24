"""Owner-run, checkpointed handover. Uses existing Google Cloud Shell credentials.

No IAM changes, public ports, credential output, source deletion or commodity fixes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from urllib.request import urlopen

PROJECT, ZONE, REGION = 'theta-pluse', 'asia-south1-a', 'asia-south1'
VM, JOB = 'personal-trading-phase1', 'theta-plus-collector'
BUCKET = 'gs://theta-pluse-theta-plus-data'
FENCE = BUCKET + '/collection/control/migration.json'
ROOT = Path(__file__).resolve().parents[2]


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def cloud(*args, capture=False):
    return run('gcloud', *args, '--project='+PROJECT, capture=capture)


def ssh(command, capture=False):
    return cloud('compute', 'ssh', VM, '--zone='+ZONE, '--tunnel-through-iap',
                 '--command='+command, capture=capture)


def remote(*args, capture=False):
    return ssh(shlex.join(args), capture=capture)


def api_module(*args, capture=False):
    return remote('sudo', 'docker', 'exec', 'personal-trading-phase1-api-1',
                  'python', '-m', *args, capture=capture)


def current_fence():
    result = subprocess.run(['gcloud', 'storage', 'objects', 'describe', FENCE,
        '--project='+PROJECT, '--format=json'], text=True, capture_output=True)
    if result.returncode:
        if re.search(r'404|not found|does not exist|No URLs matched', result.stderr, re.I):
            return {}, '0'
        raise RuntimeError('CANNOT_READ_MIGRATION_FENCE; check bucket permissions')
    metadata = json.loads(result.stdout)
    raw = cloud('storage', 'cat', FENCE, capture=True)
    return json.loads(raw), str(metadata['generation'])


def set_fence(folder, value, generation):
    path = folder / 'fence.json'
    path.write_text(json.dumps(value))
    cloud('storage', 'cp', str(path), FENCE, '--if-generation-match='+generation, capture=True)
    return current_fence()[1]


def inventory_digest():
    listing = json.loads(cloud('storage','ls',BUCKET+'/**','--json',capture=True))
    if not isinstance(listing,list): raise RuntimeError('UNSUPPORTED_STORAGE_INVENTORY_FORMAT')
    canonical = sorted(json.dumps(row,sort_keys=True,separators=(',',':')) for row in listing)
    return hashlib.sha256('\n'.join(canonical).encode()).hexdigest()


def mirror_and_import(folder, quiescent=False):
    before = inventory_digest() if quiescent else None
    mirror = folder / 'archive'
    mirror.mkdir(exist_ok=True)
    cloud('storage', 'rsync', '--recursive', BUCKET, str(mirror), capture=True)
    objects = []
    for path in sorted(mirror.rglob('*')):
        if path.is_dir(): continue
        if path.is_symlink(): raise RuntimeError('SYMLINK_IN_ARCHIVE')
        name = path.relative_to(mirror).as_posix()
        if name == 'migration-manifest.json': continue
        raw = path.read_bytes()
        objects.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    if not objects: raise RuntimeError('EMPTY_SOURCE_ARCHIVE')
    (mirror / 'migration-manifest.json').write_text(json.dumps({'source': BUCKET, 'objects': objects}))
    # No deletion switches: final pass adds/replaces only owner-exported files.
    ssh('mkdir -p -m 700 ~/theta-merge-incoming')
    cloud('compute', 'scp', '--recurse', str(mirror), VM+':~/theta-merge-incoming',
          '--zone='+ZONE, '--tunnel-through-iap', capture=True)
    # SCP directory behaviour differs if destination exists: copy into a fresh,
    # fixed-purpose transfer directory each pass, then use the known nested path.
    user_home = ssh('printf %s "$HOME"', capture=True)
    remote('sudo', 'python3', '-c',
        "from pathlib import Path;import shutil,os,sys; "
        "src=Path(sys.argv[1])/'theta-merge-incoming'; "
        "src=src/'archive' if (src/'archive'/'migration-manifest.json').exists() else src; "
        "dst=Path('/srv/personal-trading/import');shutil.copytree(src,dst,dirs_exist_ok=True); "
        "[(os.chown(p,10001,10001),os.chmod(p,0o700 if p.is_dir() else 0o600)) for p in [dst,*dst.rglob('*')]]", user_home)
    receipt = json.loads(api_module('app.merge_archive', 'import', '/legacy-import', capture=True).splitlines()[-1])
    if quiescent and inventory_digest() != before:
        raise RuntimeError('SOURCE_CHANGED_DURING_FINAL_IMPORT; source retained, handover deferred')
    return receipt


def main():
    sha = run('git', 'rev-parse', 'HEAD', capture=True)
    if not re.fullmatch('[a-f0-9]{40}', sha): raise RuntimeError('INVALID_REVISION')
    old_marker, generation = current_fence()
    if old_marker.get('state') == 'NEW_BACKEND':
        print('ALREADY_HANDED_OVER; use app.verify_phase1 live-status for diagnostics')
        return
    if old_marker.get('state') == 'FENCED':
        raise RuntimeError('PRIOR_HANDOVER_INTERRUPTED; inspect existing fence before replacing its owner')
    owner = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='theta-unified-') as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        # Upgrade the old release manager first: new images include both source trees.
        cloud('compute', 'scp', str(ROOT/'personal-trading-phase1/deploy/auto_update.py'),
              VM+':~/theta-unified-updater.py', '--zone='+ZONE, '--tunnel-through-iap', capture=True)
        ssh('sudo install -m 0755 ~/theta-unified-updater.py /usr/local/bin/personal-trading-update.py')
        remote('sudo', 'python3', '/usr/local/bin/personal-trading-update.py', '--revision', sha)
        supported = json.loads(ssh('sudo docker exec personal-trading-phase1-api-1 python -c '
            + shlex.quote("import httpx; r=httpx.get('http://127.0.0.1:8000/api/v2/system');r.raise_for_status();print(r.text)"), capture=True).splitlines()[-1])
        if supported.get('storage') != 'PostgreSQL + TimescaleDB':
            raise RuntimeError('UNIFIED_DEPLOYMENT_NOT_READY; wait for successful CI then rerun')
        # Preserve the existing credential-admin password hash when available.
        service = json.loads(cloud('run', 'services', 'describe', 'theta-plus', '--region='+REGION, '--format=json', capture=True))
        env = service.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[{}])[0].get('env',[])
        entry = next((e for e in env if e.get('name')=='THETA_TOKEN_ADMIN_HASH'), {})
        admin_hash = entry.get('value', '')
        ref = entry.get('valueFrom',{}).get('secretKeyRef',{})
        if ref:
            admin_hash = cloud('secrets', 'versions', 'access', ref.get('key','latest'), '--secret='+ref['name'], capture=True)
        if admin_hash:
            secure = folder/'legacy_admin_hash.txt';secure.write_text(admin_hash);secure.chmod(0o600)
            cloud('compute','scp',str(secure),VM+':~/theta-legacy-admin-hash.txt','--zone='+ZONE,'--tunnel-through-iap',capture=True)
            ssh('sudo cp ~/theta-legacy-admin-hash.txt /srv/personal-trading/secrets/legacy_admin_hash.txt && sudo chmod 0644 /srv/personal-trading/secrets/legacy_admin_hash.txt')
        with urlopen('https://theta-plus-jw4jpkebxa-el.a.run.app/api/migration/status', timeout=30) as response:
            if json.load(response).get('protocol') != 'unified-handover-v1':
                raise RuntimeError('LEGACY_FENCE_SUPPORT_NOT_DEPLOYED')
        print('MERGE_STAGE_1_COPY_AND_VERIFY_ARCHIVE', flush=True)
        first = mirror_and_import(folder)
        if first.get('state') != 'VERIFIED': raise RuntimeError('INITIAL_IMPORT_NOT_VERIFIED')
        paused, fenced, active = [], False, False
        try:
            print('MERGE_STAGE_2_FENCE_AND_STOP_PREVIOUS_COLLECTOR', flush=True)
            generation = set_fence(folder, {'state':'FENCED','owner':owner,'target':VM}, generation)
            fenced = True
            jobs = json.loads(cloud('scheduler','jobs','list','--location='+REGION,'--format=json',capture=True))
            for job in jobs:
                uri = job.get('httpTarget',{}).get('uri','')
                if '/jobs/'+JOB in uri and job.get('state') == 'ENABLED':
                    name = job['name'].rsplit('/',1)[-1]
                    cloud('scheduler','jobs','pause',name,'--location='+REGION,capture=True)
                    paused.append(name)
            executions = json.loads(cloud('run','jobs','executions','list','--job='+JOB,'--region='+REGION,'--format=json',capture=True))
            for item in executions:
                if not item.get('status',{}).get('completionTime'):
                    cloud('run','jobs','executions','cancel',item['metadata']['name'],'--region='+REGION,'--quiet',capture=True)
            # Confirm every execution is terminal before copying the final delta.
            for _ in range(60):
                executions = json.loads(cloud('run','jobs','executions','list','--job='+JOB,'--region='+REGION,'--format=json',capture=True))
                if all(i.get('status',{}).get('completionTime') for i in executions): break
                time.sleep(2)
            else: raise RuntimeError('LEGACY_EXECUTIONS_NOT_STOPPED')
            print('WAITING_FOR_BOUNDED_INFLIGHT_REST_WRITES', flush=True)
            for _ in range(4):
                time.sleep(30)  # SDK historical timeout is 60 seconds; retain extra write margin.
            print('MERGE_STAGE_3_FINAL_DELTA_AND_DESTINATION_CHECKSUMS', flush=True)
            final = mirror_and_import(folder, quiescent=True)
            if final.get('state') != 'VERIFIED': raise RuntimeError('FINAL_IMPORT_NOT_VERIFIED')
            print('MERGE_STAGE_4_START_NEW_LIVE_OWNER', flush=True)
            api_module('app.merge_archive','activate','--confirm-legacy-stopped')
            active = True
            remote('sudo','docker','start','personal-trading-phase1-live-1')
            for _ in range(30):
                result = json.loads(api_module('app.verify_phase1','live-status',capture=True))
                if result.get('live',{}).get('state') in ('SUBSCRIPTIONS_SENT','RECEIVING'): break
                time.sleep(2)
            else: raise RuntimeError('NEW_LIVE_CONNECTION_NOT_VERIFIED')
            set_fence(folder, {'state':'NEW_BACKEND','owner':owner,'target':VM,
                'objects':final['objects'],'manifest_sha256':final['manifest_sha256']}, generation)
            print('UNIFIED_ARCHIVE_VERIFIED_AND_LIVE_HANDOVER_COMPLETE', final['objects'])
            print('Source archive retained. MCX HOLD. All-market tick coverage is not claimed.')
            print('Open the merged website through an SSH tunnel; public URL cutover is separate:')
            print('gcloud compute ssh '+VM+' --project='+PROJECT+' --zone='+ZONE+' --tunnel-through-iap -- -N -L 8080:127.0.0.1:8000')
        except Exception:
            # Stop/fence the new live process before resuming the old account owner.
            if active:
                remote('sudo','docker','stop','personal-trading-phase1-live-1')
                api_module('app.merge_archive','deactivate')
            if fenced:
                set_fence(folder, old_marker, generation)
                for name in paused:
                    cloud('scheduler','jobs','resume',name,'--location='+REGION,capture=True)
                cloud('run','jobs','execute',JOB,'--region='+REGION,capture=True)
                print('PREVIOUS_COLLECTOR_RESTORED; imported data retained for a safe retry')
            raise


if __name__ == '__main__':
    main()
