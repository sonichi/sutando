#!/usr/bin/env python3
"""Producer for the morning-briefing Google-calendar cache (PR #2256).

`src/morning-briefing.py` is a standalone script that cannot reach the owner's
Google Workspace calendar itself, so the briefing reads a cache this module
writes: `state/calendar-today.json`. This module is the "producer" half of #2256.

Two sources, and the difference matters operationally:

* **`--from-gws`** — reads the calendar directly via the `gws` CLI, which is
  keyring-authenticated and needs no interactive step, so a cron can run it
  unattended. This is the reason the flag exists: the only documented producer
  was an agent-only connector, nothing invoked it, and on Chis-Mac-mini the
  briefing reported "couldn't read your calendar" every day from 2026-07-30 to
  2026-08-04 while `gws calendar +agenda --today` answered fine the whole time.
* **piped events / `--events-json`** — the agent supplies events it pulled from
  the connector. Unchanged.

Neither source may turn a failure into a clear day. `--from-gws` exits nonzero
and leaves any prior cache untouched if `gws` is missing, errors, returns
unparseable output, or fails on ANY calendar after another succeeded — a subset
is the falsely-clear bug (#2256) wearing a smaller number.

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


def _display_str(value: object, field: str) -> str:
    """Return `value` as a display string, or raise TypeError if it isn't one.

    `str()` on an unrendered calendar API object yields its repr, which reaches
    the owner's DM as `One meeting today: {'id': '35s817...', ...}`. Every field
    that ends up in owner-visible text goes through here so no input shape can
    coerce silently.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(
            f"calendar event `{field}` must be a rendered display string "
            f"(e.g. '8:30am-9:30am Standup' from event_to_raw()), got "
            f"{type(value).__name__}"
        )
    return value.strip()


def normalize_events(raw_events: list) -> list[dict]:
    """Coerce the documented input shapes into the reader's `{raw, calendar}` schema.

    Each element is a display string or a `{raw, calendar, start}` dict; entries
    with no usable `raw` text are dropped so an empty/garbage element never
    becomes a blank calendar line.

    Raises TypeError for any other shape — a nested connector list, a bare
    scalar, or a non-string field. Refusing aborts the whole cache write, so the
    briefing reports it could not read the calendar; an honest "unread" beats a
    plausible-looking wrong day.
    """
    events: list[dict] = []
    for ev in raw_events or []:
        if isinstance(ev, dict):
            raw = _display_str(ev.get("raw"), "raw")
            cal = _display_str(ev.get("calendar"), "calendar")
            # Optional: piped connector events carry no start, and omitting the
            # key (rather than storing "") keeps readers from special-casing it.
            start = _display_str(ev.get("start"), "start")
        elif isinstance(ev, str):
            raw, cal, start = ev.strip(), "", ""
        else:
            raise TypeError(
                "calendar event must be a display string or a {raw, calendar} "
                f"object, got {type(ev).__name__}"
            )
        if raw:
            ev_out = {"raw": raw, "calendar": cal}
            if start:
                ev_out["start"] = start
            events.append(ev_out)
    return events


def write_cache(events: list[dict], path: Path | None = None,
                today: str | None = None) -> Path:
    """Atomically write today's calendar cache. Returns the path written."""
    path = path or cache_path()
    today = today or datetime.now().strftime("%Y-%m-%d")
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_events(events)
    if missing := sum(1 for e in normalized if not e.get("start")):
        # The briefing gates BOTH "Next up" and "Last event" on every event
        # having a start, so a payload without one degrades it with no other tell.
        print(f"write_calendar_cache: {missing} of {len(normalized)} event(s) carry no "
              "`start` — the briefing will omit its 'Next up' and 'Last event' lines. "
              "Include `start` in each piped event to keep them.", file=sys.stderr)
    payload = {"date": today, "events": normalized}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)  # atomic — a concurrent briefing read never sees a partial file
    return path


class GwsUnavailable(RuntimeError):
    """`gws` could not answer — the day is UNKNOWN, never empty."""


def _gws(args: list[str], run=None) -> object:
    """Run a `gws` subcommand and return parsed JSON, or raise GwsUnavailable.

    Every failure mode raises: binary missing, non-zero exit, unparseable body,
    an API error object. None of them may fall through as "no events" — an
    unreachable calendar and a genuinely clear day must never render alike, which
    is the whole reason this cache has a `date` field and an `--empty` flag.
    """
    import subprocess
    run = run or subprocess.run
    try:
        proc = run(["gws", *args], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise GwsUnavailable("gws is not installed on this host") from exc
    if proc.returncode != 0:
        raise GwsUnavailable(f"gws {' '.join(args[:2])} exited {proc.returncode}: "
                             f"{(proc.stderr or proc.stdout or '').strip()[:200]}")
    body = (proc.stdout or "").strip()
    # `gws` prints a keyring banner before the JSON on some hosts.
    if not body.startswith(("{", "[")):
        i = min([x for x in (body.find("{"), body.find("[")) if x >= 0] or [-1])
        if i < 0:
            raise GwsUnavailable(f"gws {' '.join(args[:2])} produced no JSON")
        body = body[i:]
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise GwsUnavailable(f"gws {' '.join(args[:2])} returned unparseable JSON: {exc}") from exc
    if isinstance(data, dict) and "error" in data:
        raise GwsUnavailable(f"gws API error: {str(data['error'])[:200]}")
    return data


def _gws_all_pages(argv: list[str], params: dict | None = None, run=None,
                   max_pages: int = 50) -> list[dict]:
    """Return the `items` of EVERY page of a Google list method.

    Google marks an incomplete list response with `nextPageToken` and requires
    the request be repeated with `pageToken` set. `calendarList` defaults to 100
    entries per page and `events` to 250, so this is a supported response shape,
    not a malformed one. Reading only the first page returns a silent subset —
    which in this module is precisely the falsely-clear bug it exists to prevent,
    because a truncated read still exits 0 and can certify `events: []`.
    (@john-the-dev reproduced exactly that against this function on #2636.)

    Deliberately NOT `gws --page-all`: that flag stops at `--page-limit`
    (default 10), and against a real multi-page response I could not verify
    whether a truncated run still reports `nextPageToken`. If it does not,
    incompleteness becomes undetectable — the same failure wearing a bigger
    number. Draining here makes the terminating condition ours: we stop only
    when Google says there is no next page, and raise in every other case.

    Any page erroring raises out of `_gws`, so a mid-drain failure is total
    failure and no partial list is ever returned.
    """
    params = dict(params or {})
    items: list[dict] = []
    seen: set[str] = set()
    for _ in range(max_pages):
        args = list(argv)
        if params:
            args += ["--params", json.dumps(params)]
        data = _gws(args, run=run)
        if not isinstance(data, dict):
            raise GwsUnavailable(f"gws {' '.join(argv[:2])} returned {type(data).__name__}, not an object")
        items.extend(data.get("items") or [])
        token = data.get("nextPageToken")
        if not token:
            return items
        if token in seen:
            raise GwsUnavailable(
                f"gws {' '.join(argv[:2])} repeated a pageToken — refusing to loop on a partial read")
        seen.add(token)
        params["pageToken"] = token
    raise GwsUnavailable(
        f"gws {' '.join(argv[:2])} still had pages after {max_pages} — refusing to certify a partial read")


def _fmt_time(iso: str) -> str:
    """`2026-08-04T07:30:00-07:00` -> `7:30am` (and `8:00` -> `8am`)."""
    t = datetime.fromisoformat(iso)
    return t.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")


def event_to_raw(ev: dict) -> str:
    """Render one Google event as the reader's single `raw` line.

    All-day events carry `start.date` instead of `start.dateTime`. A DECLINED
    invitation is marked rather than dropped: the owner asked to see them, and
    silently omitting one would misreport the day just as badly as inventing it.
    """
    summary = (ev.get("summary") or "").strip() or "(no title)"
    start, end = ev.get("start") or {}, ev.get("end") or {}
    if start.get("dateTime"):
        when = f"{_fmt_time(start['dateTime'])}-{_fmt_time(end.get('dateTime') or start['dateTime'])}"
    else:
        when = "all-day"
    declined = any(
        a.get("self") and a.get("responseStatus") == "declined"
        for a in (ev.get("attendees") or [])
    )
    line = f"{when} {summary}"
    if (ev.get("location") or "").strip():
        line += f" @ {ev['location'].strip().splitlines()[0][:60]}"
    return f"{line} [DECLINED]" if declined else line


def events_from_gws(now: datetime | None = None, run=None) -> list[dict]:
    """Pull today's local-day events across EVERY selected calendar.

    Partial failure is total failure: if any calendar errors after another
    succeeded, we raise rather than write the survivors. A cache built from a
    subset is the "falsely clear" bug (#2256) wearing a smaller number — the
    owner cannot tell a 3-event day from a 15-event day with 4 calendars missing.
    """
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    tz = now.astimezone().strftime("%z")
    tz = f"{tz[:3]}:{tz[3:]}" if tz else "Z"
    lo, hi = f"{day}T00:00:00{tz}", f"{day}T23:59:59{tz}"

    cal_items = _gws_all_pages(["calendar", "calendarList", "list", "--format", "json"], run=run)
    cals = [c for c in cal_items if c.get("selected") and c.get("id")]
    if not cals:
        raise GwsUnavailable("gws returned no selected calendars — cannot certify a day")

    out: list[dict] = []
    for cal in cals:
        params = {"calendarId": cal["id"], "timeMin": lo, "timeMax": hi,
                  "singleEvents": True, "orderBy": "startTime"}
        ev_items = _gws_all_pages(["calendar", "events", "list", "--format", "json"],
                                  params=params, run=run)
        for ev in ev_items:
            if ev.get("status") == "cancelled":
                continue
            out.append({"raw": event_to_raw(ev), "calendar": cal["id"],
                        "start": (ev.get("start") or {}).get("dateTime")
                                 or (ev.get("start") or {}).get("date") or ""})
    out.sort(key=lambda e: e["start"])
    # `start` must survive: it is what lets a reader tell the day's first event
    # from the next one. Additive — readers ignore keys they don't know.
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the morning-briefing Google-calendar cache.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--events-json", help="JSON array of events (string or {raw,calendar} objects).")
    src.add_argument("--empty", action="store_true", help="Record a verified-empty day (no events).")
    src.add_argument("--from-gws", action="store_true",
                     help="Pull today's events from the `gws` CLI across every selected calendar.")
    args = ap.parse_args(argv)

    if args.empty:
        events: list = []
    elif args.from_gws:
        # A gws failure exits NONZERO and writes nothing, so the reader keeps
        # reporting "couldn't read your calendar" instead of a false clear day.
        # Zero events across calendars that all answered IS a verified-empty day
        # — that is the one case this source may certify, because it knows the
        # fetch succeeded. Piped input can never make that claim.
        try:
            events = events_from_gws()
        except GwsUnavailable as exc:
            print(f"write_calendar_cache: {exc}", file=sys.stderr)
            print("write_calendar_cache: calendar UNKNOWN — prior cache left untouched, "
                  "nothing certified.", file=sys.stderr)
            return 4
        path = write_cache(events)
        print(f"write_calendar_cache: wrote {len(events)} event(s) from gws for "
              f"{datetime.now().strftime('%Y-%m-%d')} → {path}")
        return 0
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
        try:
            events = normalize_events(parsed)
        except TypeError as e:
            print(f"write_calendar_cache: {e}", file=sys.stderr)
            print("write_calendar_cache: calendar UNKNOWN — prior cache left untouched, "
                  "nothing certified.", file=sys.stderr)
            return 2
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
