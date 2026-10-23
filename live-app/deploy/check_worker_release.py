"""Accept the rollout only after the running collector reports this build."""
import json
from pathlib import Path
import sys
import time
from urllib.request import urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from build_identity import runtime_build

URL='https://theta-plus-jw4jpkebxa-el.a.run.app/api/data-system'


def verify(fetch=None,sleep=time.sleep,attempts=36):
    if fetch is None:
        def fetch():
            with urlopen(URL,timeout=25) as response:return json.load(response)
    expected=runtime_build()
    for attempt in range(attempts):
        try:
            data=fetch()
            if (data.get('enabled') and data.get('worker_active') and data.get('worker_build_matches')
                    and data.get('worker_build_id')==expected and data.get('expected_worker_build_id')==expected):
                print('COLLECTOR_RELEASE_AND_HEARTBEAT_VERIFIED',expected)
                return True
        except Exception:pass
        if attempt+1<attempts:sleep(5)
    raise RuntimeError('Running collector release was not verified; inspect the existing job and preserved checkpoints')


if __name__=='__main__':verify()
