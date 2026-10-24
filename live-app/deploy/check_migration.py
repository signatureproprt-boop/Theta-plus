"""Read the deployed migration protocol before restarting the legacy collector."""
import json
import os
from urllib.request import urlopen

if __name__ == '__main__':
    # This deployment runner intentionally uses stdlib + gcloud, not app dependencies.
    # The fixed public endpoint exposes only the durable fence state, never secrets.
    with urlopen('https://theta-plus-jw4jpkebxa-el.a.run.app/api/migration/status', timeout=30) as response:
        data = json.load(response)
    if data.get('protocol') != 'unified-handover-v1':
        raise RuntimeError('DEPLOYED_MIGRATION_PROTOCOL_NOT_VERIFIED')
    migrated = data.get('state') in ('FENCED', 'NEW_BACKEND')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write('migrated=' + str(migrated).lower() + '\n')
    print('LEGACY_COLLECTION_HANDOVER_ACTIVE' if migrated else 'LEGACY_COLLECTION_OWNER_RETAINED')
