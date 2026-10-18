"""Server-side date/time helpers. The pod clock is UTC — anchor "today" here,
never in the browser. Strategy time logic uses Asia/Kolkata."""

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def today_iso(tz: str | None = None) -> str:
    """Today's date as YYYY-MM-DD in `tz` (default: APP_TZ env, else UTC)."""
    zone = tz or os.environ.get("APP_TZ", "UTC")
    return datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d")


def to_ist(dt: datetime, tz: str | None = None) -> datetime:
    """Normalize any datetime to an aware datetime in `tz` (default Asia/Kolkata).

    Naive datetimes are interpreted AS wall-clock time in `tz` — the documented
    convention across this codebase (tested in Phase C). Aware datetimes are
    converted.
    """
    zone = ZoneInfo(tz) if tz else IST
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def parse_hhmm(value: str) -> time:
    """Parse 'HH:MM' into a time object; raise ValueError with a clear message."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"expected numeric HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"out-of-range time {value!r}")
    return time(hour, minute)
