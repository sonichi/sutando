#!/usr/bin/env python3
"""Morning briefing for Sutando.

Runs daily at 6:57am via cron. No external credentials needed.
Sources: weather (Open-Meteo), macOS Calendar, macOS Reminders,
overnight Discord DMs, pending questions, system health.

Output: results/proactive-<ts>.txt (voice speaks it) + Discord DM.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

# Sibling scripts are launched with cwd=WORKSPACE, so their paths must not depend
# on it. Defensive only: Python >=3.11 already absolutises __file__ (bpo-20443).
_SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SRC_DIR))
from workspace_default import resolve_workspace  # noqa: E402
from util_paths import personal_path  # noqa: E402

WORKSPACE = resolve_workspace()
RESULTS_DIR = WORKSPACE / "results"
STATE_DIR = WORKSPACE / "state"

# Agent-written cache of the owner's real (Google Workspace) calendar. This
# standalone script cannot reach the Station connector, but the core agent can —
# it writes today's events here during the morning cron. See get_calendar_events.
CALENDAR_CACHE_FILE = STATE_DIR / "calendar-today.json"

# Weather codes → one-word description
WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "drizzly", 53: "drizzly", 55: "drizzly",
    61: "rainy", 63: "rainy", 65: "heavy rain",
    71: "snowy", 73: "snowy", 75: "heavy snow",
    80: "showery", 81: "showery", 82: "heavy showers",
    95: "stormy", 96: "stormy", 99: "stormy",
}


def _run_applescript(script: str, timeout: int = 8) -> tuple[str | None, str]:
    """Run an AppleScript and return (stdout, stderr).

    Returns (output_text, "") on success, (None, error_text) on failure.
    Callers that only need the output can ignore the second element.
    """
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode == 0:
            return r.stdout.strip(), ""
        return None, r.stderr.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None, ""


def get_weather() -> str:
    """Fetch current conditions from Open-Meteo (no key needed)."""
    try:
        # Default to SF; override via TZ if possible
        lat, lon = 37.77, -122.42
        tz_result, _ = _run_applescript(
            'do shell script "defaults read /Library/Preferences/com.apple.timezone"',
            timeout=3
        )
        # Use lat/lon from config (env legacy fallback) if set
        from sutando_config import config_get
        _lat_cfg, _lon_cfg = config_get("WEATHER_LAT"), config_get("WEATHER_LON")
        if _lat_cfg and _lon_cfg:
            lat = float(_lat_cfg)
            lon = float(_lon_cfg)

        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&timezone=auto&forecast_days=1&temperature_unit=fahrenheit"
        )
        with urlopen(url, timeout=8) as resp:
            d = json.loads(resp.read())
        cur = d["current"]
        day = d["daily"]
        temp = round(cur["temperature_2m"])
        code = cur["weather_code"]
        high = round(day["temperature_2m_max"][0])
        low = round(day["temperature_2m_min"][0])
        rain = day["precipitation_probability_max"][0]
        desc = WEATHER_CODES.get(code, "variable")
        rain_note = f", {rain}% chance of rain" if rain >= 30 else ""
        return f"{temp}°F and {desc}, high of {high}, low of {low}{rain_note}"
    except (URLError, KeyError, ValueError, OSError):
        return None


def _read_calendar_cache() -> list[dict] | None:
    """Read today's calendar from the agent-written Google cache.

    This script cannot reach the owner's Google Workspace calendar (the Station
    connector is agent-only), but the core agent can — during the morning cron
    it writes ``state/calendar-today.json``::

        {"date": "YYYY-MM-DD", "events": [{"raw": "9:30am Standup"}, ...]}

    Returns the events list when the cache is present and stamped for TODAY,
    else None (absent / stale / corrupt — never show yesterday's schedule).
    """
    try:
        data = json.loads(CALENDAR_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return None
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        return None
    events: list[dict] = []
    for ev in raw_events:
        start = ""
        if isinstance(ev, dict):
            raw = (ev.get("raw") or "").strip()
            cal = ev.get("calendar", "")
            start = str(ev.get("start") or "").strip()
        else:
            raw, cal = str(ev).strip(), ""
        if raw:
            out = {"raw": raw, "calendar": cal}
            # This loop rebuilds each event, so any field not named here is
            # dropped — `start` must be carried or _next_event() never fires.
            if start:
                out["start"] = start
            events.append(out)
    return events


def _google_cache_configured() -> bool:
    """True when this host has a Google-calendar cache on disk, whatever it
    holds.

    The fallback is reached when the cache is absent, stale OR corrupt, so
    only ABSENCE is evidence the owner's calendar is not in Google. A file
    that exists but cannot be parsed — truncated, half-written, or drifted
    off the schema — means blind, and a blind source is never a clear day.
    Parsing it to decide this inverted the answer on exactly those hosts.
    """
    try:
        # Lexical: a dangling symlink is a BROKEN cache, and exists() calls it
        # absent — the one filesystem shape that renders a false clear day.
        os.lstat(CALENDAR_CACHE_FILE)
    except FileNotFoundError:
        return False
    except OSError:
        # A probe that cannot answer is not evidence of absence.
        return True
    return True


def _parse_start(ev: dict):
    """Return an aware datetime for `ev['start']`, or None if absent/unparseable.

    `start` is optional by design: the gws producer supplies an ISO timestamp,
    while piped connector events and the macOS AppleScript fallback do not. A
    missing or malformed value must never be treated as "now" or as the epoch —
    either would silently reorder the day.
    """
    raw = str(ev.get("start") or "").strip()
    if not raw:
        return None
    # A bare YYYY-MM-DD is an ALL-DAY event: the DAY is known, the time of day is
    # not, and midnight would read as already-past for the rest of the day.
    if "T" not in raw and ":" not in raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:                 # naive but time-bearing: assume local
        dt = dt.astimezone()
    return dt


def _all_starts_known(events: list[dict]) -> bool:
    """True only if EVERY event has a usable start.

    Both time-based claims assert something about the whole day, so partial
    knowledge cannot support either: one unknown-time event may be upcoming.
    """
    return bool(events) and all(_parse_start(e) is not None for e in events)


def _last_event(events: list[dict]):
    """The latest event by parsed start — not list order, which may be unsorted."""
    dated = [(s, e) for e in events if (s := _parse_start(e)) is not None]
    return max(dated, key=lambda pair: pair[0])[1] if dated else None


def _next_event(events: list[dict], now=None):
    """The earliest event still ahead of `now`, or None.

    Events with no parseable start are skipped rather than assumed upcoming.
    """
    now = now or datetime.now().astimezone()
    future = [(s, e) for e in events
              if (s := _parse_start(e)) is not None and s > now]
    return min(future, key=lambda pair: pair[0])[1] if future else None


def get_calendar_events() -> list[dict] | None:
    """Get today's calendar events, preferring the owner's real Google calendar.

    Source preference:
      1. The Google-calendar cache (``state/calendar-today.json``) written by the
         core agent — the ONLY source that sees the owner's Google Workspace
         calendar, which a local macOS Calendar.app may not have subscribed.
      2. Local macOS Calendar.app via AppleScript (fallback). An EMPTY result
         from this source is only trusted on hosts that have never written a
         Google cache; where one exists, empty means blind, so None is returned.

    Returns a list of events ([] means verified empty) or None when the calendar
    could not be read — callers must not render None as "clear".

    When ``MORNING_BRIEFING_CALENDAR_SOURCE=google`` is set, the cache is the only
    TRUSTED source: if it's missing/stale, return None (→ "couldn't read your
    calendar") rather than a misleading empty read from a local calendar that
    doesn't include the work account. This is exactly the 2026-07-21 bug — the
    briefing announced "calendar is clear" off an empty local read while the
    owner had three Google meetings that day.

    Respects MORNING_BRIEFING_SKIP_CALENDARS (comma-separated list of
    calendar names to exclude, e.g. "Home,Wedding,Birthdays"). Useful for
    filtering out subscribed shared calendars that clutter the briefing
    (closes #964). Case-insensitive match on calendar name.
    """
    import os as _os

    cached = _read_calendar_cache()
    if cached is not None:
        return cached
    if _os.environ.get("MORNING_BRIEFING_CALENDAR_SOURCE", "").strip().lower() == "google":
        # Trusted source expected but unavailable — do NOT fall back to a local
        # read that can't see the work calendar and would look falsely "clear".
        print(
            "  calendar: google source expected but cache missing/stale — reporting unread",
            file=sys.stderr,
        )
        return None
    script = '''
set theDate to (current date)
set hours of theDate to 0
set minutes of theDate to 0
set seconds of theDate to 0
set endDate to theDate + (24 * 60 * 60)
set output to ""
tell application "Calendar"
    repeat with cal in every calendar
        set calName to name of cal
        set evts to (every event of cal whose start date >= theDate and start date < endDate)
        repeat with ev in evts
            set evTitle to summary of ev
            set evStart to start date of ev
            set h to hours of evStart
            set m to minutes of evStart
            set ampm to "am"
            if h >= 12 then
                set ampm to "pm"
                if h > 12 then set h to h - 12
            end if
            if h = 0 then set h to 12
            set mStr to m as text
            if m < 10 then set mStr to "0" & mStr
            set output to output & calName & "\\t" & h & ":" & mStr & ampm & " " & evTitle & "\\n"
        end repeat
    end repeat
end tell
return output
'''
    result, err = _run_applescript(script, timeout=10)
    if result is None:
        # Calendar.app not running fails the query with -600 ("Application
        # isn't running"). Launch it in the background and retry once.
        try:
            subprocess.run(["open", "-gja", "Calendar"], timeout=5)
            time.sleep(3)
        except (subprocess.TimeoutExpired, OSError):
            pass
        result, err = _run_applescript(script, timeout=10)
    if result is None:
        if err:
            print(f"  calendar: AppleScript error — {err}", file=sys.stderr)
            if "-1743" in err:
                print(
                    "  calendar: Automation permission needed. "
                    "System Settings → Privacy & Security → Automation → grant Calendar access.",
                    file=sys.stderr,
                )
        return None
    from sutando_config import config_get
    skip_cals_raw = config_get("MORNING_BRIEFING_SKIP_CALENDARS", "") or ""
    skip_cals = {c.strip().lower() for c in skip_cals_raw.split(",") if c.strip()}
    # Dedup by (time_str, title) — cross-calendar duplication (#966).
    seen: set[str] = set()
    events = []
    for line in result.splitlines():
        line = line.strip()
        if not line:
            continue
        # New format: "CalendarName\t10:30am Title"
        if "\t" in line:
            cal_name, _, event_str = line.partition("\t")
        else:
            cal_name, event_str = "", line
        # Filter by calendar skip-list (closes #964).
        if cal_name.lower() in skip_cals:
            continue
        event_str = event_str.strip()
        # Skip untitled events (closes #967): drop if nothing follows the
        # time token (AppleScript returns "10:30am " with empty title).
        parts = event_str.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            continue
        # Dedup cross-calendar events with identical time+title (#966).
        key = event_str.lower()
        if key in seen:
            continue
        seen.add(key)
        events.append({"raw": event_str, "calendar": cal_name})
    if not events and _google_cache_configured():
        # Local Calendar.app carries none of the Google work account here, so
        # an empty read is "I cannot see it", never "the day is clear".
        print(
            "  calendar: local read empty but this host has a Google cache — reporting unread",
            file=sys.stderr,
        )
        return None
    return events


def get_reminders() -> "list[str] | None":
    """Today's and overdue reminders, or None when the query could not run.

    None and [] are different facts and the caller relies on the difference:
    [] is a verified-empty list, None is "I do not know". Returning [] for a
    timeout let `synthesize()` fold an unanswered query into "Everything looks
    clean" — the same shape as the 2026-07-21 falsely-clear calendar bug
    (#2256), which is why `get_calendar_events()` already draws this line.
    """
    script_path = _SRC_DIR.parent / "skills" / "macos-tools" / "scripts" / "reminders.py"
    if not script_path.exists():
        return None
    try:
        r = subprocess.run(
            [sys.executable, str(script_path), "list", "--due-today"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return None
        items = []
        # reminders.py prints the human-readable sentinel "No reminders."
        # (exit 0) when the due-today list is empty — skip it so the empty
        # state doesn't get counted as a single reminder and rendered as
        # "Reminders due: No reminders.".
        empty_sentinels = {"no reminders.", "no reminders"}
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.lower() not in empty_sentinels:
                items.append(line)
        return _demote_stale_reminders(items)[:5]
    except (subprocess.TimeoutExpired, OSError):
        return None


# TWO years, not one: the due clause gives only year granularity, so `year_now - 1`
# would demote a December item in January — one month overdue, not a year.
_STALE_YEARS = 2
# Anchored to the literal `(due` so a lowercase "due" in a TITLE cannot supply the year;
# any 4-digit run, because an alternation of plausible years cannot match a corrupt one.
_DUE_YEAR_RE = re.compile(r"\(due\b[^)]*?\b(\d{4})\b")


def _reminder_due_year(line):
    """The year in a reminder's `(due …)` clause, or None. Takes the LAST clause: the
    real one is appended, so a title containing `(due …)` cannot win."""
    m = _DUE_YEAR_RE.findall(line)
    return int(m[-1]) if m else None


def _demote_stale_reminders(items, now=None):
    """Move reminders overdue by >= two calendar years to the END. Never drops; an
    unparseable date keeps its position so a format change cannot bury a live one."""
    year_now = (datetime.fromtimestamp(now) if now else datetime.now()).year
    cutoff = year_now - _STALE_YEARS
    fresh, stale = [], []
    for it in items:
        y = _reminder_due_year(it)
        (stale if (y is not None and y <= cutoff) else fresh).append(it)
    return fresh + stale


def get_overnight_discord(now: float | None = None) -> list[str]:
    """Owner Discord DMs from the last 8 hours, newest last (max 5).

    Reads the TASK FILES the bridge writes, not `logs/discord-bridge.log`.

    The log cannot answer this question. Its docstring promised an 8-hour window
    and the code computed `cutoff = time.time() - 8 * 3600` and then never used
    it: the effective window was `splitlines()[-200:]`, a line count. That is not
    an oversight that a one-line patch fixes — the `[msg] #DM` lines carry no
    timestamp at all (measured 2026-08-02: 10 of 6,754 lines in the live log had
    an ISO stamp, and none of them were message lines), so there is nothing for a
    time cutoff to compare against. Meanwhile the line window silently shrinks as
    the bridge gets chattier: on the same log, all 9 matching DM lines sat at
    indices 596-6177 while the window began at 6,554, so the briefing reported
    ZERO overnight messages on a day that had them. The failure direction is
    false-clean, which is the worst one for a daily briefing.

    The bridge already writes a properly timestamped record of every DM it
    processes: `tasks/task-<id>.txt`, archived to `tasks/archive/` after
    handling, each carrying an ISO `timestamp:`, `source:`, `channel_name:` and
    `access_tier:`. Reading those makes the promised 8-hour filter actually
    implementable, with no new instrumentation.

    Owner DMs are `source: discord` + `channel_name: DM` + `access_tier: owner`.
    The tier check is what replaces the old sender-name exclusion: peer bots post
    to shared channels as `team`, so they cannot reach this list.
    """
    now = time.time() if now is None else now
    cutoff = now - 8 * 3600
    tasks_dir = WORKSPACE / "tasks"
    archive = tasks_dir / "archive"
    found: list[tuple[float, str]] = []
    # The archive is MONTH-PARTITIONED (`tasks/archive/YYYY-MM/<id>.txt`, PR
    # #591); the flat form is legacy and only holds tasks archived before it.
    # Scanning the flat form alone reproduces the very false-clean this function
    # exists to fix: measured on the live workspace, 280 flat vs 178 month-
    # partitioned, and in an 8-hour window 1 owner DM flat vs 2 missed. One
    # level deep, matching discord-bridge.py's own `archive.glob(f"*/{id}.txt")`
    # — not rglob, which would walk unbounded depth.
    globs = ((tasks_dir, "task-*.txt"),
             (archive, "task-*.txt"),
             (archive, "*/task-*.txt"))
    for directory, pattern in globs:
        if not directory.is_dir():
            continue
        for path in directory.glob(pattern):
            try:
                head = path.read_text(errors="replace")
            except OSError:
                continue
            fields = dict(re.findall(r"^([a-z_]+):[ \t]*(.*)$", head, re.M))
            if fields.get("source") != "discord":
                continue
            if fields.get("channel_name") != "DM":
                continue
            if fields.get("access_tier", "owner") != "owner":
                continue
            stamp = fields.get("timestamp", "")
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            # BOTH edges. Only the lower one was enforced, so a single
            # future-dated timestamp counted as "overnight" in every briefing
            # until the wall clock caught up — unbounded, and the mirror image
            # of the false-clean this function exists to fix. The risk is not
            # theoretical now that briefing truth rests on mutable on-disk
            # stamps: clock skew, a hand-edited file, or imported state all
            # produce one. Both bounds inclusive: `cutoff` is 8h ago exactly,
            # and a task written this instant must still count.
            if not cutoff <= when <= now:
                continue
            body = ""
            m = re.search(r"^task:[ \t]*(.*)$", head, re.M)
            if m:
                body = m.group(1).strip()[:80]
            found.append((when, body))
    found.sort()
    return [body for _when, body in found[-5:]]


def _load_notifier():
    """Load check-pending-questions.py once, as a module.

    Module level on purpose: loading it inside get_pending_questions() would make
    the predicate unreachable to tests, which point the notifier at a fixture by
    swapping `PQ_FILE` on the loaded module (the pattern
    tests/check-pending-questions-open-status.test.py already uses). A per-call
    load rebuilds a private copy every time, so a test can only ever exercise a
    re-implementation of the delegation instead of the shipped function — which is
    exactly how the first version of this change shipped a regression past its own
    test. Its main() is __name__-guarded, so importing fires no notification.
    """
    import importlib.util

    src = _SRC_DIR / "check-pending-questions.py"
    spec = importlib.util.spec_from_file_location("_cpq_predicate", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CPQ = _load_notifier()


#: The briefing is SPOKEN (voice reads results/proactive-morning-*.txt) as well as
#: DM'd, so a title clipped mid-word is read aloud as a mid-word fragment. A hard
#: `title[:60]` produced, from a real 2026-08-02 run:
#:     "WIRE - awaiting your verdict / steer (no urgency; nothing bl"
#: - cut inside "blocked", and leaving an unmatched "(" so the parenthetical never
#: closes. Clip on a word boundary instead, and drop a parenthetical that the clip
#: left open rather than speaking half of it.
def _cut_at_imbalance(s: str) -> str:
    """Truncate `s` at the first bracket that cannot be matched, either way.

    Both directions matter and only one was handled before. Stripping a leading
    "(" shifts the slice window one character right, which can pull the matching
    ")" into the output ALONE -- the mirror image of the orphan being removed:

        clip_for_speech("(abcdefghi) long trailing title", 11) -> "abcdefghi)..."

    A single left-to-right pass covers both: an unmatched ")" truncates before
    itself; anything still open at the end truncates before the FIRST unmatched
    "(" (not the last -- cutting at the first is what guarantees balance when
    several are open).
    """
    stack: list[int] = []
    for i, ch in enumerate(s):
        if ch == "(":
            stack.append(i)
        elif ch == ")":
            if not stack:
                return s[:i]
            stack.pop()
    return s[:stack[0]] if stack else s


#: The briefing is SPOKEN (voice reads results/proactive-morning-*.txt) as well as
#: DM'd, so a title clipped mid-word is read aloud as a mid-word fragment. A hard
#: `title[:60]` produced, from a real 2026-08-02 run:
#:     "WIRE - awaiting your verdict / steer (no urgency; nothing bl"
#: - cut inside "blocked", and leaving an unmatched "(" so the parenthetical never
#: closes. Clip on a word boundary instead, and never emit an unbalanced bracket.
def clip_for_speech(text: str, limit: int) -> str:
    """Clip to <= limit chars without cutting a word or orphaning a bracket.

    Returns text unchanged when it already fits, so the common case is untouched
    -- including text that is ALREADY unbalanced at the source. The contract is
    "clipping must not create an orphan", not "rewrite titles we did not clip".
    """
    text = text.strip()
    if len(text) <= limit:
        return text

    def _shorten(s: str) -> str:
        # limit - 1 reserves the ellipsis, so the result is never over the limit.
        head = s[:limit - 1]
        cut = head.rfind(" ")
        if cut > 0:
            head = head[:cut]
        return _cut_at_imbalance(head).rstrip(" ,;:-\u2014/(")

    head = _shorten(text)
    if not head:
        # The whole window sat inside a parenthetical opening at character 0.
        # Drop the bracket and re-shorten -- through the SAME balance guard, which
        # is what the first version of this fix missed.
        head = _shorten(text.lstrip("(").lstrip())
    return head + "\u2026"


def get_pending_questions() -> list[str]:
    """Return unanswered questions, delegating to check-pending-questions.py.

    That module's `get_waiting_questions()` is the single source of truth for
    "is this question still waiting". This function used to re-implement the
    predicate, and the two copies drifted: on 2026-07-28 the notifier counted 33
    and this counted 32. The missing entry was a live owner ask
    ("/observe MVP: design fully resolved, build on your nod") dropped because
    the local copy tested `'RESOLVED' in title.upper()` — a substring match that
    fires on the word appearing anywhere in the prose, including in "NOT
    self-resolved". An open question that goes uncounted goes unsurfaced.

    Fixing only this copy would leave the duplicate in place to re-diverge —
    #2351 had already fixed the notifier's side (`Status: open`) without this one
    changing. So the predicate now lives in exactly one place.

    That invariant was initially only half-true: this function still dropped
    organizer shells and inline `[RESOLVED ...]` titles locally, so the two
    consumers reported different counts (notifier 2 / briefing 1 on a corpus with
    one active marker plus one open ask) — review finding on 919c35f2. Both
    classifications now live in the shared parser, and nothing here judges
    waiting-ness; this function only maps the result to display titles.

    Deliberately no fallback parser: a second implementation is the bug. And a
    failure here must not degrade to `[]`, which the briefing would render as the
    confident "no pending questions" that this whole class of bug produces.
    """
    # The briefing resolves its OWN file and hands it to the predicate, rather
    # than relying on the notifier's independent resolution. Two reasons: the two
    # modules could otherwise read different files on a host where resolution
    # differs, silently reintroducing the divergence this change removes; and it
    # keeps `personal_path` as the single patch point the existing regression
    # tests already use (tests/briefing-pending-status.test.py,
    # tests/morning-briefing-pending-extract.test.py), so the seam does not move.
    _CPQ.PQ_FILE = personal_path("pending-questions.md", WORKSPACE)

    out: list[str] = []
    for q in _CPQ.get_waiting_questions():
        title = (q.get("title") or q.get("id") or "") if isinstance(q, dict) else str(q)
        title = re.sub(r'^\[\d{4}-\d{2}-\d{2}\]\s*', '', title.strip())
        if not title:
            continue
        out.append(clip_for_speech(title, 60))
    return out



def get_health_issues() -> "list[str] | None":
    """Failed health items, or None when the check could not run.

    Same contract as `get_reminders`: [] means the check ran and found nothing,
    None means it did not run. A timed-out health check returning [] made the
    briefing assert a clean system it had never inspected.
    """
    hc = _SRC_DIR / "health-check.py"
    if not hc.exists():
        return None
    try:
        r = subprocess.run(
            [sys.executable, str(hc)],
            capture_output=True, text=True, timeout=30,
            cwd=str(WORKSPACE)
        )
        issues = []
        for line in r.stdout.splitlines():
            if "✗" in line:  # only real failures, not warns (warns are expected/known)
                # Format: "  ✗ <name>   <status>   <detail>"
                # Strip the symbol and collapse whitespace
                clean = re.sub(r'^\s*[✗⚠]\s*', '', line)
                # Split on 2+ spaces to get name, status, detail
                parts = re.split(r'\s{2,}', clean.strip())
                if len(parts) >= 3:
                    name, status, detail = parts[0], parts[1], parts[2]
                    issues.append(f"{name}: {detail}")
                elif parts:
                    issues.append(parts[0])
        # A non-zero exit is AMBIGUOUS here and must not be read as failure
        # alone: health-check.py ends in `sys.exit(1 if issues else 0)`, so
        # non-zero is its normal way of saying "I found problems" — and those
        # problems are exactly what this function is for. The crash case is
        # non-zero WITH nothing parseable (import error, traceback on stderr,
        # empty stdout): the run produced no verdict at all, so the answer is
        # "unknown", not "clean".
        if r.returncode != 0 and not issues:
            return None
        return issues[:3]
    except (subprocess.TimeoutExpired, OSError):
        return None


def synthesize(weather, events, reminders, discord_msgs, pending_qs, health_issues) -> str:
    now = datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    parts = [f"{greeting}."]

    # Weather
    if weather:
        parts.append(f"It's {weather}.")

    # Calendar — None means the query failed (distinct from verified empty).
    if events is None:
        parts.append("I couldn't read your calendar this morning.")
    elif events:
        count = len(events)
        if count == 1:
            parts.append(f"One meeting today: {events[0]['raw']}.")
        else:
            upcoming = _next_event(events) if _all_starts_known(events) else None
            last = _last_event(events) if _all_starts_known(events) else None
            if upcoming is not None:
                parts.append(f"{count} meetings today. Next up: {upcoming['raw']}.")
            elif last is not None:
                # Every start known and all past — naming one implies it is ahead.
                parts.append(f"{count} meetings today, all earlier — "
                             f"last was {last['raw']}.")
            else:
                # Incomplete start times: claim neither, keep the prior wording.
                parts.append(f"{count} meetings today. First up: {events[0]['raw']}.")
    else:
        parts.append("Your calendar is clear today.")

    # Reminders
    if reminders:
        r_list = ", ".join(reminders[:3])
        parts.append(f"Reminders due: {r_list}.")

    # Pending questions
    if pending_qs:
        if len(pending_qs) == 1:
            parts.append(f"One pending question waiting: {pending_qs[0]}.")
        else:
            parts.append(f"{len(pending_qs)} pending questions. Top item: {pending_qs[0]}.")

    # Overnight Discord
    if discord_msgs:
        parts.append(f"Overnight: {len(discord_msgs)} Discord message{'s' if len(discord_msgs) > 1 else ''}.")

    # Health issues
    if health_issues:
        issues_str = "; ".join(health_issues[:2])
        parts.append(f"System note: {issues_str}.")

    # Closing — every input must be VERIFIED empty, not merely falsy. `None`
    # from any gather means that query did not run, and an unanswered query is
    # not evidence of a clean day. Previously only the calendar was checked this
    # way, so a timed-out reminders fetch and a timed-out health check (both
    # returning [] at the time) produced a confident "Everything looks clean"
    # over two questions nobody had answered.
    if (events == [] and reminders == [] and health_issues == []
            and not pending_qs):
        parts.append("Everything looks clean. Good day for deep work.")

    return " ".join(parts)


def completion_line(result_file, narrative: str) -> str:
    """What the run actually accomplished.

    This script WRITES a result file; delivery is a channel bridge's job and may
    never happen — no bridge running, or no channel configured on the host. The
    previous wording ("Briefing delivered:") reported an outcome this script does
    not observe and cannot verify, so a run that reached nobody looked identical
    to one that reached the owner. Observed 2026-07-21: six proactive results,
    the oldest 8h old, sat undrained while every run printed "delivered".

    Extracted so the claim is testable rather than an inline literal (same shape
    as `summary_line` in health-check.py).
    """
    return (f"Briefing written to {result_file.name} — delivery depends on a "
            f"channel bridge draining results/:\n{narrative}")


def main():
    # Check sentinel — don't repeat if already run today
    today = datetime.now().strftime("%Y-%m-%d")
    sentinel = STATE_DIR / f"morning-briefing-{today}.sentinel"
    if sentinel.exists() and "--force" not in sys.argv:
        print(f"Morning briefing already generated today ({today}). Use --force to re-run.")
        return

    print("Gathering morning briefing...")

    # Gather all sources (skip errors silently)
    weather = get_weather()
    print(f"  weather: {weather or 'unavailable'}")

    events = get_calendar_events()
    print(f"  calendar: {'unavailable' if events is None else f'{len(events)} events'}")

    reminders = get_reminders()
    print(f"  reminders: {'unavailable' if reminders is None else f'{len(reminders)} due'}")

    discord_msgs = get_overnight_discord()
    print(f"  discord overnight: {len(discord_msgs)} messages")


    pending_qs = get_pending_questions()
    print(f"  pending questions: {len(pending_qs)}")

    health_issues = get_health_issues()
    print(f"  health issues: {'unavailable' if health_issues is None else len(health_issues)}")

    # Synthesize
    narrative = synthesize(weather, events, reminders, discord_msgs, pending_qs, health_issues)

    # Write voice result
    ts = int(time.time() * 1000)
    result_file = RESULTS_DIR / f"proactive-morning-{ts}.txt"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # Privacy: the briefing carries calendar + email — private data that must
    # go to the owner's DM only. The `[dm-only]` marker forces DM delivery and
    # suppresses any `[channel:]` redirect at the bridge, so this can never be
    # posted to a shared channel (result_markers.parse_markers). The marker is
    # stripped before delivery/voice, so the owner never sees it.
    result_file.write_text(f"[dm-only]\n{narrative}")
    print(f"  → {result_file.name}")

    # Mark as done today
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(datetime.now().isoformat())

    print("\n" + completion_line(result_file, narrative))

    # Anonymous, opt-out product telemetry: one bucketed event when this feature
    # actually runs (not on the already-delivered early return). No content/PII.
    try:  # pragma: no cover — bounded flush; logic tested in tests/telemetry.test.py
        from telemetry import feature_used  # sibling module (src/ on sys.path)

        feature_used("morning_briefing", flush=True)
    except Exception:  # pragma: no cover — telemetry must never break the feature
        pass


if __name__ == "__main__":
    main()
