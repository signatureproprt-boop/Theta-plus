"""Phase A — validators: staleness / validity / completeness checks.

The validators never raise on market-data problems; they report them, so the
strategy can degrade to WAIT instead of crashing (failure-handling rule).
"""

from __future__ import annotations

from datetime import datetime

from lib.dates import to_ist
from models.market_models import DataHealth, HealthCode, InstrumentTick, MarketSnapshot, OptionChain

OK = HealthCode.OK.value
STALE = HealthCode.STALE.value
INVALID = HealthCode.INVALID.value
MISSING = HealthCode.MISSING.value
INCOMPLETE = HealthCode.INCOMPLETE.value


def is_stale(timestamp: datetime, now: datetime, max_age_seconds: float) -> bool:
    """Naive timestamps are interpreted in IST (see lib.dates.to_ist)."""
    age = (to_ist(now) - to_ist(timestamp)).total_seconds()
    return age > max_age_seconds


def data_age_seconds(timestamp: datetime, now: datetime) -> float:
    return max((to_ist(now) - to_ist(timestamp)).total_seconds(), 0.0)


def tick_issues(tick: InstrumentTick, now: datetime, max_age_seconds: float) -> list[str]:
    """Human-readable issues for an index tick (empty list = healthy)."""
    issues: list[str] = []
    if is_stale(tick.timestamp, now, max_age_seconds):
        issues.append(f"DATA_AGE {STALE}: age {data_age_seconds(tick.timestamp, now):.0f}s > {max_age_seconds:.0f}s")
    if tick.close <= 0 or tick.open <= 0 or tick.high <= 0 or tick.low <= 0:
        issues.append(f"PRICE {INVALID}: non-positive OHLC (close={tick.close})")
    if tick.high < tick.low:
        issues.append(f"PRICE {INVALID}: high < low")
    if tick.volume < 0:
        issues.append(f"VOLUME {INVALID}: negative volume")
    return issues


def chain_issues(chain: OptionChain, now: datetime, max_age_seconds: float) -> list[str]:
    """Human-readable issues for an option chain (empty list = healthy)."""
    issues: list[str] = []
    if is_stale(chain.timestamp, now, max_age_seconds):
        issues.append(f"DATA_AGE {STALE}: chain age {data_age_seconds(chain.timestamp, now):.0f}s")
    if not chain.rows:
        issues.append(f"OPTION_CHAIN {INCOMPLETE}: empty option chain")
        return issues
    for row in chain.rows:
        if row.ce.ltp < 0 or row.pe.ltp < 0:
            issues.append(f"OPTION_CHAIN {INVALID}: negative LTP at strike {row.strike}")
        if row.ce.oi < 0 or row.pe.oi < 0:
            issues.append(f"OPTION_CHAIN {INVALID}: negative OI at strike {row.strike}")
    zero_oi = [r.strike for r in chain.rows if r.ce.oi == 0 and r.pe.oi == 0]
    if zero_oi:
        issues.append(f"OPTION_CHAIN {INCOMPLETE}: zero OI at strikes {zero_oi[:5]}")
    return issues


def snapshot_health(snapshot: MarketSnapshot, now: datetime, max_age_seconds: float) -> DataHealth:
    """Snapshot-level health (age + option chain). Feature-level engines extend
    this with PCR/VWAP/OI validity in engines/feature_engine.py."""
    checks: dict[str, str] = {"DATA_AGE": OK, "OPTION_CHAIN": OK}
    details: list[str] = []
    status = OK

    def absorb(code: str, component: str, detail: str) -> None:
        nonlocal status
        checks[component] = code
        details.append(detail)
        if code == INVALID or (status == OK and code in (STALE, MISSING, INCOMPLETE)):
            status = code
        elif code != OK and status != INVALID:
            status = code

    age = data_age_seconds(snapshot.tick.timestamp, now)
    if age > max_age_seconds:
        absorb(STALE, "DATA_AGE", f"data age {age:.0f}s exceeds {max_age_seconds:.0f}s")
    for issue in tick_issues(snapshot.tick, now, max_age_seconds):
        # Staleness is already recorded under DATA_AGE above; classifying it a
        # second time as PRICE/INVALID would escalate a STALE feed to ERROR.
        if issue.startswith("DATA_AGE"):
            continue
        absorb(INVALID, "PRICE", issue)
    chain_ok = True
    for issue in chain_issues(snapshot.chain, now, max_age_seconds):
        code = STALE if issue.startswith("DATA_AGE") else (INCOMPLETE if "INCOMPLETE" in issue else INVALID)
        chain_ok = False
        absorb(code, "OPTION_CHAIN", issue)
    if chain_ok:
        checks["OPTION_CHAIN"] = OK
    return DataHealth(status=HealthCode(status), age_seconds=age, checks=checks, details=tuple(details))


def aggregate_health(checks: dict[str, str]) -> HealthCode:
    """Worst status wins; INVALID outranks MISSING > STALE > INCOMPLETE > OK."""
    severity = {OK: 0, INCOMPLETE: 1, STALE: 2, MISSING: 3, INVALID: 4}
    worst = max(checks.values(), key=lambda c: severity.get(c, 5)) if checks else OK
    return HealthCode(worst)
