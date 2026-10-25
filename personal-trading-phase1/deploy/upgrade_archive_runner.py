"""Owner-run upgrade of an archive download, with phase and identity checks.

Never interrupts import, fencing, collector handover or a process from another repo.
The source archive and verified checkpoints are retained.
"""
import json
from pathlib import Path
import signal
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
LOG = Path.home() / 'theta-merge.log'
STATUS = 'https://theta-plus-jw4jpkebxa-el.a.run.app/api/migration/status'


def matching_processes():
    expected = str(ROOT / 'personal-trading-phase1/deploy/merge_cloudshell.py')
    result = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = (path / 'cmdline').read_bytes().split(b'\0')
            if expected.encode() in args and Path(args[0].decode()).name == 'python3':
                result.append(int(path.name))
        except (FileNotFoundError, PermissionError, IndexError):
            continue
    return result


def download_phase(log):
    # The parent is stopped while inspecting this bounded log tail.
    with log.open('rb') as stream:
        stream.seek(max(0, log.stat().st_size - 65536))
        lines = stream.read().decode(errors='replace').splitlines()
    last = next((line for line in reversed(lines) if line.strip()), '')
    return last.startswith('ARCHIVE_COPY_PROGRESS ')


def has_children(pid, proc_root=None):
    tasks = (proc_root or Path('/proc')) / str(pid) / 'task'
    for task in tasks.iterdir():
        try:
            if (task / 'children').read_text().strip():
                return True
        except FileNotFoundError:
            # A downloader thread can finish between listing and reading.
            continue
    return False


def stop_download(pid):
    stopped = False
    try:
        # Do not strand a gcloud child midway through a remote command.
        for _ in range(20):
            if not has_children(pid):
                signal_pid(pid, signal.SIGSTOP)
                stopped = True
                if not has_children(pid):
                    break
                signal_pid(pid, signal.SIGCONT)
                stopped = False
            time.sleep(1)
        else:
            raise RuntimeError('UPGRADE_DEFERRED_ACTIVE_CHILD; existing process retained')
        if not download_phase(LOG):
            raise RuntimeError('UPGRADE_DEFERRED_NOT_DOWNLOAD_PHASE; existing process retained')
        with urlopen(STATUS, timeout=20) as response:
            status = json.load(response)
        if status.get('protocol') != 'unified-handover-v1' or status.get('state') != 'LEGACY_BACKEND':
            raise RuntimeError('UPGRADE_DEFERRED_HANDOVER_STATE; existing process retained')
        # SIGTERM is delivered upon SIGCONT. SQLite journal recovery and hashes
        # make already written files reusable; no market/source records are removed.
        signal_pid(pid, signal.SIGTERM)
        signal_pid(pid, signal.SIGCONT)
        stopped = False
        for _ in range(50):
            path = Path('/proc') / str(pid) / 'stat'
            try:
                if path.read_text().rsplit(')', 1)[1].strip().split()[0] == 'Z':
                    return
            except FileNotFoundError:
                return
            time.sleep(0.2)
        raise RuntimeError('PRIOR_DOWNLOAD_NOT_STOPPED; no second process started')
    finally:
        if stopped:
            signal_pid(pid, signal.SIGCONT)


def signal_pid(pid, kind):
    import os
    os.kill(pid, kind)


def main():
    processes = matching_processes()
    if len(processes) > 1:
        raise RuntimeError('MULTIPLE_DOWNLOAD_PROCESSES_REVIEW_REQUIRED')
    if processes:
        stop_download(processes[0])
        print('PREVIOUS_DOWNLOAD_STOPPED; verified files retained', flush=True)
    subprocess.run(['bash', str(ROOT / 'personal-trading-phase1/deploy/start_merge_cloudshell.sh')],
        cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
