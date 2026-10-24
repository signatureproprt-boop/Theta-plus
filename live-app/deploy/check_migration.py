"""Do not restart the old collector after an owner-authorized migration."""
import os
from google.cloud import storage
from google.api_core.exceptions import NotFound
import json

if __name__ == '__main__':
    try:
        raw = storage.Client().bucket('theta-pluse-theta-plus-data').blob(
            'collection/control/migration.json').download_as_text(timeout=30)
        migrated = json.loads(raw).get('state') in ('FENCED', 'NEW_BACKEND')
    except NotFound:
        migrated = False
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write('migrated=' + str(migrated).lower() + '\n')
    print('LEGACY_COLLECTION_HANDOVER_ACTIVE' if migrated else 'LEGACY_COLLECTION_OWNER_RETAINED')
