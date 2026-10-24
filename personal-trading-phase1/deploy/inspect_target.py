"""Read-only target discovery with the existing GitHub cloud identity."""
import json
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT = 'theta-pluse'


def main():
    token = subprocess.check_output(['gcloud', 'auth', 'print-access-token'],
                                    text=True, stderr=subprocess.PIPE).strip()
    request = Request(f'https://compute.googleapis.com/compute/v1/projects/{PROJECT}/aggregated/instances',
                      headers={'Authorization': 'Bearer ' + token})
    try:
        with urlopen(request, timeout=30) as response:
            body = json.load(response)
    except HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {}
        error = body.get('error', {})
        reasons = [d.get('reason') for d in error.get('details', []) if d.get('reason')]
        print(json.dumps({'state': 'VM_DISCOVERY_BLOCKED', 'http_status': exc.code,
                          'status': error.get('status'), 'reasons': reasons,
                          'required_read_permission': 'compute.instances.list'}))
        raise SystemExit(2)
    instances = [vm for zone in body.get('items', {}).values() for vm in zone.get('instances', [])]
    print(json.dumps({'state': 'VM_DISCOVERY_COMPLETE', 'project': PROJECT,
                      'instances': [{'name': vm['name'], 'zone': vm['zone'].rsplit('/', 1)[-1],
                                     'status': vm.get('status'), 'labels': vm.get('labels', {})}
                                    for vm in instances]}))


if __name__ == '__main__':
    main()
