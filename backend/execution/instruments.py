"""Phase H — validated instrument mapping (§9/§11/§25).

A Dhan `securityId` is NEVER guessed. It must come from an explicit mapping
supplied by the operator (JSON file at `DHAN_INSTRUMENT_MAP_PATH`, or an
in-process mapping used by tests). When the option is not present in the
mapping, resolution fails and the execution gate blocks the order with
SECURITY_ID_UNAVAILABLE.

Expected JSON shape (one entry per tradable option contract):

    [
      {"underlying": "NIFTY", "expiry": "2026-01-29", "strike": 25050,
       "option_type": "CALL", "security_id": "43492", "trading_symbol": "...",
       "exchange_segment": "NSE_FNO", "lot_size": 75, "tradable": true}
    ]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from models.execution_models import InstrumentRef

OPTION_TYPES = {"CE": "CALL", "PE": "PUT", "CALL": "CALL", "PUT": "PUT"}


class InstrumentMap:
    """Read-only lookup over operator-supplied instrument rows."""

    def __init__(self, rows: Optional[Iterable[dict]] = None) -> None:
        self._refs: list[InstrumentRef] = []
        for row in rows or []:
            try:
                self._refs.append(InstrumentRef(**row))
            except Exception:
                continue  # an invalid row is ignored, never partially trusted

    @classmethod
    def from_env(cls) -> "InstrumentMap":
        path = os.environ.get("DHAN_INSTRUMENT_MAP_PATH", "")
        if not path:
            return cls([])
        file = Path(path)
        if not file.exists():
            return cls([])
        try:
            data = json.loads(file.read_text())
        except Exception:
            return cls([])
        return cls(data if isinstance(data, list) else data.get("instruments", []))

    @property
    def configured(self) -> bool:
        return bool(self._refs)

    def resolve(
        self, underlying: str, strike: Optional[int], option_side: str, expiry: str = ""
    ) -> Optional[InstrumentRef]:
        """Exact match only: underlying + strike + option type (+ expiry if given)."""
        wanted = OPTION_TYPES.get((option_side or "").upper())
        if wanted is None or strike is None:
            return None
        for ref in self._refs:
            if ref.underlying != underlying or ref.strike != strike:
                continue
            if ref.option_type != wanted:
                continue
            if expiry and ref.expiry and ref.expiry != expiry:
                continue
            return ref
        return None


def validate_instrument(ref: Optional[InstrumentRef], quantity: int, ltp: Optional[float]) -> tuple[bool, str]:
    """Option + quantity + price validation before any submission (§11/§24/§25)."""
    if ref is None:
        return False, "no validated instrument mapping for this option"
    if not ref.security_id:
        return False, "mapping row has no securityId"
    if not ref.tradable:
        return False, f"instrument {ref.trading_symbol or ref.security_id} is not marked tradable"
    if ltp is None or ltp <= 0 or ltp != ltp or ltp in (float("inf"), float("-inf")):
        return False, "option LTP is missing, zero, negative or not finite"
    if ref.lot_size and quantity % ref.lot_size != 0:
        return False, f"quantity {quantity} is not a multiple of lot size {ref.lot_size}"
    return True, ""


# --------------------------------------------------------------------------
# Phase J — instrument-map validation (§3). Nothing is guessed or defaulted.
# --------------------------------------------------------------------------
REQUIRED_MAP_FIELDS = ("security_id", "exchange_segment", "instrument_type", "expiry",
                       "strike", "option_type")
CANONICAL_KINDS = ("NIFTY", "NIFTY FUTURE", "NIFTY CE", "NIFTY PE")
VALID_SEGMENTS = ("IDX_I", "NSE_EQ", "NSE_FNO", "BSE_FNO", "NSE_CURRENCY",
                  "BSE_CURRENCY", "MCX_COMM", "BSE_EQ")
VALID_INSTRUMENT_TYPES = ("INDEX", "FUTIDX", "OPTIDX", "FUTSTK", "OPTSTK", "EQUITY")


def validate_instrument_map(rows) -> dict:
    """Structural validation of an operator-supplied mapping.

    Returns a report; an absent/empty mapping is PENDING (never PASS). This does
    NOT prove the ids are correct at the broker — that needs a live instrument
    master lookup, which requires Dhan credentials.
    """
    rows = list(rows or [])
    errors: list[str] = []
    kinds: set[str] = set()
    if not rows:
        return {
            "status": "PENDING", "rows": 0, "valid_rows": 0, "errors": (),
            "canonical_kinds_present": (), "canonical_kinds_missing": CANONICAL_KINDS,
            "broker_verified": False,
            "note": ("INSTRUMENT_MAPPING = PENDING: no mapping supplied "
                     "(DHAN_INSTRUMENT_MAP_PATH). Live execution stays blocked with "
                     "SECURITY_ID_UNAVAILABLE."),
        }

    valid = 0
    for i, row in enumerate(rows):
        missing = [f for f in REQUIRED_MAP_FIELDS if f not in row or row[f] in ("", None)]
        instrument_type = str(row.get("instrument_type", "")).upper()
        option_type = str(row.get("option_type", "")).upper()
        if instrument_type == "FUTIDX":
            missing = [m for m in missing if m not in ("strike", "option_type")]
        if instrument_type == "INDEX":
            missing = [m for m in missing if m not in ("strike", "option_type", "expiry")]
        if missing:
            errors.append(f"row {i}: missing {', '.join(missing)}")
            continue
        segment = str(row.get("exchange_segment", "")).upper()
        if segment not in VALID_SEGMENTS:
            errors.append(f"row {i}: unknown exchange_segment {segment!r}")
            continue
        if instrument_type not in VALID_INSTRUMENT_TYPES:
            errors.append(f"row {i}: unknown instrument_type {instrument_type!r}")
            continue
        if instrument_type == "OPTIDX":
            if option_type not in OPTION_TYPES:
                errors.append(f"row {i}: option_type {option_type!r} is not CE/PE/CALL/PUT")
                continue
            try:
                if int(row["strike"]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"row {i}: strike {row.get('strike')!r} is not a positive number")
                continue
            kinds.add("NIFTY CE" if OPTION_TYPES[option_type] == "CALL" else "NIFTY PE")
        elif instrument_type == "FUTIDX":
            kinds.add("NIFTY FUTURE")
        elif instrument_type == "INDEX":
            kinds.add("NIFTY")
        valid += 1

    missing_kinds = tuple(k for k in CANONICAL_KINDS if k not in kinds)
    status = "PASS" if (valid == len(rows) and not missing_kinds) else "FAIL"
    return {
        "status": status,
        "rows": len(rows),
        "valid_rows": valid,
        "errors": tuple(errors[:20]),
        "canonical_kinds_present": tuple(sorted(kinds)),
        "canonical_kinds_missing": missing_kinds,
        "broker_verified": False,
        "note": ("structural validation only — securityId correctness at the broker "
                 "requires Dhan credentials and an instrument-master lookup (PENDING)"),
    }


def instrument_map_status() -> dict:
    """Status of the env-configured mapping, used by the readiness checklist."""
    path = os.environ.get("DHAN_INSTRUMENT_MAP_PATH", "").strip()
    if not path:
        return validate_instrument_map([])
    file = Path(path)
    if not file.exists():
        report = validate_instrument_map([])
        return {**report, "status": "FAIL",
                "note": f"DHAN_INSTRUMENT_MAP_PATH set but file not found: {file.name}"}
    try:
        data = json.loads(file.read_text())
    except Exception as exc:
        report = validate_instrument_map([])
        return {**report, "status": "FAIL", "note": f"mapping file is not valid JSON: {type(exc).__name__}"}
    rows = data if isinstance(data, list) else data.get("instruments", [])
    return validate_instrument_map(rows)
