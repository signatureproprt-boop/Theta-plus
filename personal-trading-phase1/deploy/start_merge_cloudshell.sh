#!/usr/bin/env bash
# Owner-run launcher. Browser disconnections do not terminate the nohup process.
# A Cloud Shell host shutdown still stops execution; verified downloads persist
# in normal (non-ephemeral) home storage and are reused on the next invocation.
set -euo pipefail
umask 077
THETA_SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$THETA_SOURCE_ROOT"
if pgrep -f '[p]ython3 -u .*merge_cloudshell.py' >/dev/null; then
    echo 'MERGE_PROCESS_PRESENT; inspect existing log instead of starting twice'
    exit 0
fi
# Complete any owner authorization/key prompts interactively before detaching.
gcloud auth print-access-token --project=theta-pluse >/dev/null
gcloud compute ssh personal-trading-phase1 --project=theta-pluse \
    --zone=asia-south1-a --tunnel-through-iap --command=true
THETA_MERGE_LOG="$HOME/theta-merge.log"
touch "$THETA_MERGE_LOG"
chmod 600 "$THETA_MERGE_LOG"
printf '\nMERGE_BACKGROUND_START %s\n' "$(date -u +%FT%TZ)" >> "$THETA_MERGE_LOG"
nohup python3 -u "$THETA_SOURCE_ROOT/personal-trading-phase1/deploy/merge_cloudshell.py" \
    >> "$THETA_MERGE_LOG" 2>&1 < /dev/null &
echo "MERGE_BACKGROUND_PID $!"
echo 'Progress: tail -n 20 "$HOME/theta-merge.log"'
