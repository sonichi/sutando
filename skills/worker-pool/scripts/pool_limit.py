"""Reset times printed by a provider-limit pane; unrecognized text is unknown."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A yearless date further than this from now names the neighbouring year.
_HALF_YEAR = timedelta(days=182)


class Reset(NamedTuple):
    at: float | None
    banner: str


def reset(text, now):
    """The banner's reset words and their time (None when unparsed); None without a banner."""
    matches = list(re.finditer(r"(?:resets?|continuing automatically)\s+(?:at\s+)?([^\n]+)", text, re.I))
    if not matches:
        return None
    banner = matches[-1].group(1).split("·")[0].strip()
    return Reset(_parse(banner, now), banner)


def reset_at(text, now):
    found = reset(text, now)
    return found.at if found else None


def _parse(value, now):
    zone = re.search(r"\(([^)]+)\)", value)
    try:
        tz = ZoneInfo(zone[1]) if zone else datetime.fromtimestamp(now).astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        return None
    value = re.sub(r"\s*\([^)]+\)", "", value).strip()
    today = datetime.fromtimestamp(now, tz)
    tomorrow = value.lower().startswith("tomorrow ")
    explicit_today = value.lower().startswith("today ")
    value = re.sub(r"^(?:today|tomorrow)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+at\s+", " ", value, flags=re.I)
    value = re.sub(r"\s+(am|pm)$", r"\1", value, flags=re.I)
    for pattern in ("%b %d %I:%M%p", "%b %d %I%p", "%b %d, %I:%M%p", "%b %d, %I%p", "%I:%M%p", "%I%p", "%H:%M",
                    "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(f"{today.year} {value}", f"%Y {pattern}") if "%Y" not in pattern else datetime.strptime(value, pattern)
        except ValueError:
            continue
        if "%Y" not in pattern:
            parsed = parsed.replace(year=today.year)
        if "%b" in pattern and "%Y" not in pattern:
            local = today.replace(tzinfo=None)
            step = 1 if parsed < local - _HALF_YEAR else -1 if parsed > local + _HALF_YEAR else 0
            try:
                parsed = parsed.replace(year=parsed.year + step)
            except ValueError:
                return None
        time_only = "%b" not in pattern and "%Y" not in pattern
        if time_only:
            parsed = parsed.replace(month=today.month, day=today.day)
        parsed = parsed.replace(tzinfo=parsed.tzinfo or tz)
        if tomorrow or (time_only and not explicit_today and parsed.timestamp() <= now):
            parsed += timedelta(days=1)
        return parsed.timestamp()
    return None
