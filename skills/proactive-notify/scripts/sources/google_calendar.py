"""Google Calendar source — wraps `gws calendar +agenda` for upcoming events.

Returns events starting within `lookahead_min` of now, each as a dict with:
- title
- start_iso (ISO 8601 with timezone)
- start_local (HH:MM string in local TZ)
- minutes_until (int — minutes between now and event start)
- calendar
- location
- description (best-effort; may be empty since +agenda doesn't include it)
- dedup_key_suffix (event-uniqueness — start_iso + title)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Iterator


GWS_BIN = os.environ.get("GWS_BIN") or shutil.which("gws") or "gws"


def _parse_event_start(start_str: str) -> datetime:
    # All-day events come as "YYYY-MM-DD"; timed events as ISO with offset.
    if len(start_str) == 10:
        return datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(start_str)


def _iter_raw_events(lookahead_min: int) -> Iterator[dict]:
    days = max(1, (lookahead_min + 1440 - 1) // 1440)
    cmd = [GWS_BIN, "calendar", "+agenda", "--days", str(days), "--format", "json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0:
        return
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return
    yield from payload.get("events", [])


def _owner_calendar_set(config: dict) -> "set[str] | None":
    """Return a normalized set of allowed calendar identifiers, or None (allow all).

    Config key ``owner_calendars`` — list of calendar names / email aliases that
    belong to the owner. Events whose ``calendar`` field does not match any entry
    are skipped (#964). Comparison is case-insensitive.

    Example config fragment::

        owner_calendars:
          - khilo@live.ca
          - Bassil Khilo
          - primary
    """
    raw = config.get("owner_calendars")
    if not raw:
        return None
    return {str(c).strip().lower() for c in raw if c}


def fetch(config: dict) -> list[dict]:
    lookahead = int(config.get("lookahead_min", 15))
    now = datetime.now(timezone.utc)
    owner_cals = _owner_calendar_set(config)
    results = []
    for raw in _iter_raw_events(lookahead):
        start_str = raw.get("start")
        if not start_str:
            continue
        try:
            start_dt = _parse_event_start(start_str)
        except ValueError:
            continue
        # All-day events (start length == 10) skip — no minute-precise reminder.
        if len(start_str) == 10:
            continue
        delta_sec = (start_dt - now).total_seconds()
        minutes_until = int(delta_sec // 60)
        if minutes_until < 0 or minutes_until > lookahead:
            continue
        title = (raw.get("summary") or "").strip()
        if not title:
            continue  # skip untitled holds — #967; upgrade to attendee rendering later
        # Skip events from other people's subscribed calendars (#964).
        cal = (raw.get("calendar") or "").strip()
        if owner_cals is not None and cal.lower() not in owner_cals:
            continue
        results.append({
            "title": title,
            "start_iso": start_str,
            "start_local": start_dt.astimezone().strftime("%H:%M %Z"),
            "minutes_until": minutes_until,
            "calendar": raw.get("calendar") or "",
            "location": raw.get("location") or "",
            "description": "",
            "dedup_key_suffix": f"{start_str}|{title}",
        })
    # Dedup by (start_iso, title) — same meeting appears once per subscribed calendar (#966).
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        key = r["dedup_key_suffix"]
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped
