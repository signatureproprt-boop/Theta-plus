"""Instrument mapping built from DhanHQ's OFFICIAL scrip master (no guessing).

Source (public, no credentials needed, per the current official docs):
    https://images.dhan.co/api-data/api-scrip-master.csv

Columns used: SEM_EXM_EXCH_ID, SEM_SEGMENT, SEM_SMST_SECURITY_ID,
SEM_INSTRUMENT_NAME, SEM_TRADING_SYMBOL, SEM_LOT_UNITS, SEM_EXPIRY_DATE,
SEM_STRIKE_PRICE, SEM_OPTION_TYPE, SEM_EXPIRY_FLAG.

Every security_id written here comes from that file — the strategy never
invents one, and `execution/instruments.validate_instrument_map()` re-validates
whatever this produces.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
NIFTY_INDEX_SECURITY_ID = "13"
NIFTY_SYMBOL_PREFIX = "NIFTY-"


class InstrumentMasterError(RuntimeError):
    """The official scrip master could not be fetched or parsed."""


def fetch_scrip_master(timeout: float = 60.0) -> str:
    """Download the official compact scrip master CSV (public endpoint)."""
    try:
        resp = httpx.get(SCRIP_MASTER_URL, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise InstrumentMasterError(f"scrip master download failed: {exc}") from exc
    if resp.status_code >= 400:
        raise InstrumentMasterError(f"scrip master returned HTTP {resp.status_code}")
    if "SEM_SMST_SECURITY_ID" not in resp.text[:2000]:
        raise InstrumentMasterError("scrip master payload is missing the expected header")
    return resp.text


def _expiry_date(row: dict[str, str]) -> str:
    return (row.get("SEM_EXPIRY_DATE") or "")[:10]


def _is_nifty(row: dict[str, str]) -> bool:
    return (row.get("SEM_TRADING_SYMBOL") or "").upper().startswith(NIFTY_SYMBOL_PREFIX)


def build_nifty_instrument_map(
    csv_text: str, on: Optional[date] = None, expiry: str = ""
) -> dict[str, Any]:
    """Canonical NIFTY mapping: index, nearest future, and every CE/PE of one expiry.

    `expiry` empty => the nearest non-past OPTIDX expiry in the master file.
    """
    rows: Iterable[dict[str, str]] = csv.DictReader(io.StringIO(csv_text))
    today = (on or datetime.now().date()).isoformat()

    index_row = None
    futures: list[dict[str, str]] = []
    options: list[dict[str, str]] = []
    for row in rows:
        if row.get("SEM_EXM_EXCH_ID") != "NSE":
            continue
        kind = row.get("SEM_INSTRUMENT_NAME")
        if kind == "INDEX" and (row.get("SEM_TRADING_SYMBOL") or "").strip().upper() == "NIFTY":
            index_row = row
        elif kind == "FUTIDX" and _is_nifty(row):
            futures.append(row)
        elif kind == "OPTIDX" and _is_nifty(row):
            options.append(row)

    if index_row is None or not options:
        raise InstrumentMasterError("scrip master contained no NIFTY index/OPTIDX rows")

    expiries = sorted({_expiry_date(r) for r in options if _expiry_date(r) >= today})
    if not expiry:
        if not expiries:
            raise InstrumentMasterError("no non-expired NIFTY option expiry in the scrip master")
        expiry = expiries[0]

    chosen_options = [r for r in options if _expiry_date(r) == expiry]
    if not chosen_options:
        raise InstrumentMasterError(f"scrip master has no NIFTY options for expiry {expiry}")

    future_expiries = sorted({_expiry_date(r) for r in futures if _expiry_date(r) >= today})
    chosen_future = next(
        (r for r in futures if future_expiries and _expiry_date(r) == future_expiries[0]), None
    )

    instruments: list[dict[str, Any]] = [{
        "kind": "NIFTY",
        "underlying": "NIFTY",
        "trading_symbol": index_row.get("SEM_TRADING_SYMBOL", "NIFTY"),
        "security_id": str(index_row["SEM_SMST_SECURITY_ID"]),
        "exchange_segment": "IDX_I",
        "instrument_type": "INDEX",
        "expiry": "",
        "strike": 0,
        "option_type": "",
        "lot_size": 1,
        "tradable": False,  # the index itself is not tradable
    }]

    if chosen_future is not None:
        instruments.append({
            "kind": "NIFTY FUTURE",
            "underlying": "NIFTY",
            "trading_symbol": chosen_future.get("SEM_TRADING_SYMBOL", ""),
            "security_id": str(chosen_future["SEM_SMST_SECURITY_ID"]),
            "exchange_segment": "NSE_FNO",
            "instrument_type": "FUTIDX",
            "expiry": _expiry_date(chosen_future),
            "strike": 0,
            "option_type": "",
            "lot_size": int(float(chosen_future.get("SEM_LOT_UNITS") or 0)),
            "tradable": True,
        })

    for row in sorted(chosen_options, key=lambda r: (float(r.get("SEM_STRIKE_PRICE") or 0),
                                                     r.get("SEM_OPTION_TYPE", ""))):
        side = (row.get("SEM_OPTION_TYPE") or "").upper()
        if side not in ("CE", "PE"):
            continue
        instruments.append({
            "kind": f"NIFTY {side}",
            "underlying": "NIFTY",
            "trading_symbol": row.get("SEM_TRADING_SYMBOL", ""),
            "security_id": str(row["SEM_SMST_SECURITY_ID"]),
            "exchange_segment": "NSE_FNO",
            "instrument_type": "OPTIDX",
            "expiry": _expiry_date(row),
            "strike": int(float(row.get("SEM_STRIKE_PRICE") or 0)),
            "option_type": "CALL" if side == "CE" else "PUT",
            "lot_size": int(float(row.get("SEM_LOT_UNITS") or 0)),
            "expiry_flag": row.get("SEM_EXPIRY_FLAG", ""),
            "tradable": True,
        })

    return {
        "source": SCRIP_MASTER_URL,
        "source_type": "OFFICIAL_DHAN_SCRIP_MASTER",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "underlying": "NIFTY",
        "underlying_security_id": str(index_row["SEM_SMST_SECURITY_ID"]),
        "expiry": expiry,
        "available_expiries": expiries[:12],
        "broker_verified": False,  # true only after a live Dhan API confirmation
        "instruments": instruments,
    }


def write_instrument_map(mapping: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(mapping, indent=1))
    return target
