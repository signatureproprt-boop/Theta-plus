"""Build the NIFTY instrument map from DhanHQ's official scrip master.

    cd /app/backend && python scripts/build_instrument_map.py [--expiry YYYY-MM-DD]
                                                              [--out /app/backend/data/nifty_instrument_map.json]

Needs no credentials (the scrip master is a public official file). Point
DHAN_INSTRUMENT_MAP_PATH at the output file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.dhan_instruments import (  # noqa: E402
    build_nifty_instrument_map,
    fetch_scrip_master,
    write_instrument_map,
)
from execution.instruments import validate_instrument_map  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expiry", default="")
    ap.add_argument("--out", default="/app/backend/data/nifty_instrument_map.json")
    args = ap.parse_args()

    mapping = build_nifty_instrument_map(fetch_scrip_master(), expiry=args.expiry)
    report = validate_instrument_map(mapping["instruments"])
    path = write_instrument_map(mapping, args.out)
    print(f"expiry={mapping['expiry']} instruments={len(mapping['instruments'])} -> {path}")
    print(f"validation={report['status']} valid_rows={report['valid_rows']} "
          f"kinds={report['canonical_kinds_present']} errors={report['errors'][:3]}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
