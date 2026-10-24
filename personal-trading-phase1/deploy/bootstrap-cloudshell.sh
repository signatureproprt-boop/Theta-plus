#!/usr/bin/env bash
# One-time owner bootstrap. Future verified releases are installed by the VM timer.
set -euo pipefail
PROJECT=theta-pluse
ZONE=asia-south1-a
NAME=personal-trading-phase1
REVISION=
CREATE=no
while test "$#" -gt 0; do
  case "$1" in
    --revision) REVISION=$2; shift 2 ;;
    --create-vm) CREATE=yes; shift ;;
    *) echo 'Usage: bootstrap-cloudshell.sh --revision VERIFIED_SHA --create-vm'; exit 2 ;;
  esac
done
[[ "$REVISION" =~ ^[a-f0-9]{40}$ ]]
test "$CREATE" = yes || { echo 'Explicit --create-vm is required for billable VM creation.'; exit 2; }
echo 'Target: theta-pluse / asia-south1-a / personal-trading-phase1'
echo 'Creates a billable e2-standard-2 VM (2 vCPU, 8 GiB), 20 GB boot and 50 GB persistent data disk.'
echo 'API stays private on loopback. No existing Theta Plus resources or IAM bindings are changed.'
python3 - "$REVISION" <<'PY'
import json,sys,urllib.request
sha=sys.argv[1]
base='https://api.github.com/repos/signatureproprt-boop/Theta-plus'
def read(path):
    with urllib.request.urlopen(urllib.request.Request(base+path,headers={'User-Agent':'phase1-bootstrap'}),timeout=30) as r:
        return json.load(r)
runs=read('/actions/runs?head_sha='+sha+'&per_page=30')['workflow_runs']
accepted=False
for run in runs:
    if run['path']!='.github/workflows/personal-trading-phase1.yml' or run['head_branch']!='main' or run['event'] not in ('push','workflow_dispatch'):
        continue
    jobs=read('/actions/runs/'+str(run['id'])+'/jobs?per_page=100')['jobs']
    accepted=any(j['name']=='Verify real Docker Timescale Redis and Celery integration' and j['conclusion']=='success' for j in jobs)
    break
if not accepted:
    raise SystemExit('Docker integration must pass for this exact revision before provisioning. No cloud resources changed.')
print('Exact source revision passed Docker integration.')
PY
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
BASE="https://raw.githubusercontent.com/signatureproprt-boop/Theta-plus/$REVISION/personal-trading-phase1/deploy"
curl -fsSL "$BASE/vm_startup.sh" -o "$TEMP/startup-body.sh"
{
  printf '#!/usr/bin/env bash\nexport PHASE1_REVISION=%q\n' "$REVISION"
  cat "$TEMP/startup-body.sh"
} > "$TEMP/startup.sh"
gcloud services enable compute.googleapis.com iap.googleapis.com oslogin.googleapis.com --project="$PROJECT"
# Refuse to reconfigure a pre-existing VM automatically.
if gcloud compute instances describe "$NAME" --project="$PROJECT" --zone="$ZONE" >/dev/null 2>&1; then
  echo 'Target VM already exists. No instance changes made. Inspect its bootstrap logs and update timer.'
  exit 0
fi
TAG=personal-trading-phase1-iap
ensure_rule() {
  local rule=$1 priority=$2 action=$3 source=$4
  if gcloud compute firewall-rules describe "$rule" --project="$PROJECT" --format=json > "$TEMP/rule.json" 2>/dev/null; then
    python3 - "$TEMP/rule.json" "$priority" "$action" "$source" "$TAG" <<'PY'
import json,sys
path,priority,action,source,tag=sys.argv[1:]
rule=json.load(open(path))
assert rule.get('network','').rsplit('/',1)[-1]=='default'
assert rule.get('direction')=='INGRESS' and rule.get('priority')==int(priority)
assert rule.get('sourceRanges')==[source] and rule.get('targetTags')==[tag]
assert not rule.get('disabled',False)
key='allowed' if action=='ALLOW' else 'denied'
assert rule.get(key)==[{'IPProtocol':'tcp','ports':['22']}]
print('Existing dedicated firewall rule matches requested restrictions.')
PY
  else
    gcloud compute firewall-rules create "$rule" --project="$PROJECT" --network=default --direction=INGRESS --priority="$priority" --action="$action" --rules=tcp:22 --source-ranges="$source" --target-tags="$TAG"
  fi
}
ensure_rule personal-trading-phase1-iap-ssh 900 ALLOW 35.235.240.0/20
ensure_rule personal-trading-phase1-deny-public-ssh 950 DENY 0.0.0.0/0
gcloud compute instances create "$NAME" --project="$PROJECT" --zone="$ZONE" --machine-type=e2-standard-2 --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=20GB --boot-disk-type=pd-balanced --no-boot-disk-auto-delete --create-disk=name=personal-trading-phase1-data,size=50GB,type=pd-balanced,auto-delete=no,device-name=phase1-data --network=default --tags="$TAG" --labels=app=personal-trading-phase1,managed-by=verified-git-updater --no-service-account --no-scopes --metadata=block-project-ssh-keys=TRUE,enable-oslogin=TRUE --metadata-from-file=startup-script="$TEMP/startup.sh"
echo 'VM created; Docker installation and CI-verified deployment are running automatically.'
echo 'Read installation progress:'
echo 'gcloud compute ssh personal-trading-phase1 --project=theta-pluse --zone=asia-south1-a --tunnel-through-iap --command="sudo tail -n 80 /var/log/personal-trading-bootstrap.log"'
echo 'Then configure the Dhan credentials through the protected interactive VM session; no token is copied from Theta Plus.'
