#!/usr/bin/env python3
"""Producer for the morning-briefing Google-calendar cache (PR #2256).

`src/morning-briefing.py` is a standalone script that CANNOT reach the owner's
Google Workspace calendar — the Station/Composio connector is agent-only. So
the briefing reads a cache the *agent* writes: `state/calendar-today.json`.
This module is that writer — the missing "producer" half of #2256.

Contract (matches `morning-briefing.get_calendar_events()` /
`_read_calendar_cache()`): the file is

    {"date": "YYYY-MM-DD", "events": [{"raw": "9:00-9:30am · 1:1 w/ Sam", "calendar": "work"}, ...]}

`date` is today in **local** time (same `datetime.now().strftime("%Y-%m-%d")`
the reader checks), so a stale cache from yesterday is ignored rather than shown.
`events == []` is a *verified-empty* day (the reader renders "clear" only for a
real empty read from the trusted source, never for a missing/absent cache).

Usage (the agent pipes the events it pulled from the Google connector):

    echo '[{"raw":"9:00-9:30am 1:1 w/ Sam","calendar":"work"}]' \\
        | python3 src/write_calendar_cache.py

    python3 src/write_calendar_cache.py --events-json '[...]'
    python3 src/write_calendar_cache.py --empty          # verified no events today

Each element may be a string (used as `raw`) or an object with `raw` (+ optional
`calendar`). The write is atomic (tmp + os.replace) so a concurrent briefing read
never sees a half-written file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402


def cache_path() -> Path:
    return resolve_workspace() / "state" / "calendar-today.json"


def normalize_events(raw_events: list) -> list[dict]:
    """Coerce assorted input shapes into the reader's `{raw, calendar}` schema.

    Accepts strings or dicts; drops entries with no usable `raw` text so an
    empty/garbage element never becomes a blank calendar line.
    """
    events: list[dict] = []
    for ev in raw_events or []:
        if isinstance(ev, dict):
            raw = str(ev.get("raw") or "").strip()
            cal = str(ev.get("calendar") or "").strip()
        else:
            raw, cal = str(ev).strip(), ""
        if raw:
            events.append({"raw": raw, "calendar": cal})
    return events


def write_cache(events: list[dict], path: Path | None = None,
                today: str | None = None) -> Path:
    """Atomically write today's calendar cache. Returns the path written."""
    path = path or cache_path()
    today = today or datetime.now().strftime("%Y-%m-%d")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": today, "events": normalize_events(events)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)  # atomic — a concurrent briefing read never sees a partial file
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the morning-briefing Google-calendar cache.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--events-json", help="JSON array of events (string or {raw,calendar} objects).")
    src.add_argument("--empty", action="store_true", help="Record a verified-empty day (no events).")
    args = ap.parse_args(argv)

    if args.empty:
        events: list = []
    else:
        blob = args.events_json if args.events_json is not None else sys.stdin.read()
        blob = (blob or "").strip()
        if not blob:
            print("write_calendar_cache: no events given (use --empty for a verified-empty day)",
                  file=sys.stderr)
            return 2
        try:
            parsed = json.loads(blob)
        except ValueError as e:
            print(f"write_calendar_cache: invalid JSON — {e}", file=sys.stderr)
            return 2
        if not isinstance(parsed, list):
            print("write_calendar_cache: expected a JSON array of events", file=sys.stderr)
            return 2
        # Honesty guard (#2256): normalize BEFORE writing so a payload that maps to
        # zero usable events never becomes a verified-empty ("clear") day. A
        # Google-API-shaped list like [{"summary":"1:1","start":{...}}] has no `raw`
        # key, so it normalizes to [] — writing that as events:[] is the exact
        # false-"clear" this producer exists to prevent. Only --empty may certify an
        # empty day; here we fail nonzero and leave any prior cache untouched.
        events = normalize_events(parsed)
        if not events:
            print("write_calendar_cache: input contained no usable events — refusing to "
                  "certify a verified-empty day (use --empty for a genuinely empty day). "
                  "Prior cache left untouched.", file=sys.stderr)
            return 3

    path = write_cache(events)
    n = len(normalize_events(events))
    print(f"write_calendar_cache: wrote {n} event(s) for {datetime.now().strftime('%Y-%m-%d')} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
