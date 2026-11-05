"""Reset times printed by a provider-limit pane; unrecognized text is unknown."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def reset_at(text, now):
    matches = list(re.finditer(r"(?:resets?|continuing automatically)\s+(?:at\s+)?([^\n]+)", text, re.I))
    if not matches:
        return None
    value = matches[-1].group(1).split("·")[0].strip()
    zone = re.search(r"\(([^)]+)\)", value)
    try:
        tz = ZoneInfo(zone[1]) if zone else datetime.fromtimestamp(now).astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        return None
    value = re.sub(r"\s*\([^)]+\)", "", value).strip()
    today = datetime.fromtimestamp(now, tz)
    tomorrow = value.lower().startswith("tomorrow ")
    value = re.sub(r"^(?:today|tomorrow)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+at\s+", " ", value, flags=re.I)
    value = re.sub(r"\s+(am|pm)$", r"\1", value, flags=re.I)
    for pattern in ("%b %d %I:%M%p", "%b %d %I%p", "%I:%M%p", "%I%p", "%H:%M",
                    "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(f"{today.year} {value}", f"%Y {pattern}") if "%Y" not in pattern else datetime.strptime(value, pattern)
        except ValueError:
            continue
        if "%Y" not in pattern:
            parsed = parsed.replace(year=today.year)
        if "%b" not in pattern and "%Y" not in pattern:
            parsed = parsed.replace(month=today.month, day=today.day)
        if tomorrow:
            parsed += timedelta(days=1)
        return parsed.replace(tzinfo=parsed.tzinfo or tz).timestamp()
    return None
