#!/usr/bin/env bash
# One-time official SDK + independent private collector deployment.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
bash deploy/release-existing.sh
python3 deploy/setup_worker.py
python3 deploy/check-sdk.py
