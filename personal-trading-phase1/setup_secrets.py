"""Run locally on your VPS. Secrets never need to be sent to ChatGPT."""
import getpass
import os
import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    folder = root / 'secrets'
    folder.mkdir(exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    for name in ('postgres_password', 'api_key'):
        path = folder / (name + '.txt')
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48) + '\n')
        # Container UIDs differ. Parent directory is owner-only on the host;
        # files are mounted read-only into only the selected containers.
        path.chmod(0o644)
    admin_hash = folder / 'legacy_admin_hash.txt'
    if not admin_hash.exists():
        admin_hash.write_text('')
    admin_hash.chmod(0o644)
    client_id = input('Dhan Client ID (blank to defer authentication): ').strip()
    token = getpass.getpass('Dhan Access Token (hidden; blank to defer): ').strip()
    for name, value in [('dhan_client_id', client_id), ('dhan_access_token', token)]:
        path = folder / (name + '.txt')
        if value or not path.exists():
            # Write in place: existing Compose bind mounts keep this inode.
            path.write_text(value + '\n')
        path.chmod(0o644)
        if os.geteuid() == 0:
            os.chown(path, 10001, 10001)
    env = root / '.env'
    if not env.exists():
        env.write_text((root / '.env.example').read_text())
        env.chmod(0o600)
    print('Configuration ready. Credentials were not printed.')


if __name__ == '__main__':
    main()
