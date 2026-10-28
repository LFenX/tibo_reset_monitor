"""IANA time zones for message stamps and the calendar.

The config cascade picks one zone per target. Display text uses that zone's
offset at the instant being shown, including daylight-saving changes.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("tibo_reset_monitor")

UTC = timezone.utc
_DATE_ONLY = 10


def is_valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def zone_for(name: str | None, default: str = "UTC") -> tzinfo:
    """Load an IANA zone. Unknown names fall back to ``default``, then UTC."""
    for candidate in (name, default, "UTC"):
        if not candidate:
            continue
        text = str(candidate).strip()
        if text.upper() == "UTC":
            return UTC
        try:
            return ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return UTC


def system_timezone() -> tuple[str, bool]:
    """Return ``(name, fell_back_to_utc)``.

    Prefer ``TZ``, then the ``/etc/localtime`` zoneinfo path, then the
    process zone's IANA key. ``fell_back_to_utc`` is true only when none of
    those is an IANA name.
    """
    for name in _system_zone_candidates():
        canonical = _canonical_zone(name)
        if canonical == "UTC" or is_valid_zone(canonical):
            return canonical, False
    try:
        local = datetime.now().astimezone()
    except Exception:
        local = None
    if local is not None and local.tzname() in {"UTC", "GMT"} and local.utcoffset() == timedelta(0):
        return "UTC", False
    return "UTC", True


def _system_zone_candidates() -> list[str]:
    found: list[str] = []
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        found.append(tz_env)
    localtime = _zone_from_localtime()
    if localtime:
        found.append(localtime)
    try:
        key = getattr(datetime.now().astimezone().tzinfo, "key", None)
    except Exception:
        key = None
    if isinstance(key, str) and key.strip():
        found.append(key.strip())
    return found


def _zone_from_localtime() -> str | None:
    try:
        path = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = f"{os.sep}zoneinfo{os.sep}"
    if marker not in path:
        return None
    return path.split(marker, 1)[1]


def _canonical_zone(name: str) -> str:
    if name in {"UTC", "Etc/UTC", "Etc/GMT", "GMT"}:
        return "UTC"
    return name


def offset_label(value: datetime) -> str:
    """``UTC+8``, ``UTC-7``, ``UTC+5:30``, or ``UTC+0`` for the instant's real offset."""
    offset = value.utcoffset()
    if offset is None:
        return "UTC+0"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if seconds:
        return f"UTC{sign}{hours}:{minutes:02d}:{seconds:02d}"
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def parse_instant(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text or _is_date_only(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_local(value: str, zone: tzinfo) -> str:
    """``2026-10-28 14:29:38 UTC+8``, or empty when ``value`` is not a timestamp."""
    parsed = parse_instant(value)
    if parsed is None:
        return ""
    local = parsed.astimezone(zone)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {offset_label(local)}"


def local_now(now: datetime, zone: tzinfo) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(zone)


def local_day(value: object, zone: tzinfo) -> str | None:
    """Calendar date in ``zone``.

    A ``YYYY-MM-DD`` string is kept as written. A timestamp is converted to the
    zone and then reduced to a date, so the offset at that instant matters.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _is_date_only(text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    parsed = parse_instant(text)
    if parsed is None:
        try:
            return date.fromisoformat(text[:_DATE_ONLY]).isoformat()
        except ValueError:
            return None
    return parsed.astimezone(zone).date().isoformat()


def month_of(value: datetime, zone: tzinfo) -> tuple[int, int]:
    local = local_now(value, zone)
    return local.year, local.month


def _is_date_only(text: str) -> bool:
    return len(text) == _DATE_ONLY and text[4:5] == "-" and text[7:8] == "-" and "T" not in text
