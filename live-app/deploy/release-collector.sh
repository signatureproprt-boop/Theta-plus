#!/usr/bin/env bash
# One repair release, followed by checkpoint-safe collector activation.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bash deploy/release-existing.sh "$@"
python3 deploy/activate_worker.py
