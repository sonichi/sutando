#!/usr/bin/env python3
"""
Sutando calendar writer — adds an event to the local macOS Calendar via AppleScript.

Usage (``--owner-asked`` on every run, and only when the owner asked for this event
in this conversation; without it the script refuses, exit 2):
  python3 calendar-writer.py add "Dentist" "2026-03-17 14:00" "2026-03-17 15:00" --owner-asked
  python3 calendar-writer.py add "Standup" 2026-03-17T09:00 2026-03-17T09:15 \\
      --calendar Work --notes "Room 4" [--key standup-0317] --owner-asked

A write needs the flag itself: the host opt-in and SUTANDO_ALLOW_NATIVE_PIM=1 allow
reads only. Inside a core session (Claude or Codex) the bound task must also be
readable and owner-tier: with no binding, or one that cannot be read, it exits 2.

Times are local, ``YYYY-MM-DD HH:MM`` (or ``T`` between). Without ``--calendar`` the
event goes to the only writable calendar; with several, the script lists them and
adds nothing. The request is echoed on stderr before anything is written.

The event carries a key (``--key``, else derived from title, times and calendar) as
a ``[sutando-key:…]`` line in its notes. An event found with that key at that start
time is reported as ``already_exists`` instead of added again, and runs for one key
on this host take turns through a lock file. That lowers the chance of a duplicate;
it is not a guarantee, so after any failure check Calendar before retrying.

The event is read back by uid: JSON on stdout and exit 0 only when title, times and
calendar match the request. A macOS denial (-1743) exits 3; every other failure
exits 1 with the cause on stderr.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Sibling helper: the scripts run by path, so their directory is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_pim_consent as consent  # noqa: E402

APP = "Calendar"
TIMEOUT = 30
SEP = "|||"
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
KEY_FORMAT = re.compile(r"[A-Za-z0-9._-]{1,64}")
USAGE = ('Usage: python3 calendar-writer.py add "<title>" "<YYYY-MM-DD HH:MM>" "<YYYY-MM-DD HH:MM>" '
         "[--calendar NAME] [--notes TEXT] [--key KEY] --owner-asked")

WRITE_NEEDS_FLAG = (
    f"Calendar: nothing was added. Adding an event needs {consent.FLAG} on this run, passed only "
    f"when the owner asked for this event in this conversation; the host opt-in and "
    f"{consent.ENV}=1 allow reading, not writing. {consent.ORDER}"
)
UNVERIFIED = (
    "Calendar: nothing was added. This session does not say whose task is running, so the "
    "owner's consent could not be verified and the local Calendar app was not opened. Ask the "
    "owner: they can add the event themselves, or run this command in their own terminal."
)

# Dates are built from numbers and read back as numbers: a date string depends on the locale.
HANDLERS = """
on makeDate(y, m, d, secs)
    set dt to current date
    set day of dt to 1
    set year of dt to (y as integer)
    set month of dt to (m as integer)
    set day of dt to (d as integer)
    set time of dt to (secs as integer)
    return dt
end makeDate

on stamp(dt)
    return ((year of dt) as string) & "-" & ((month of dt as integer) as string) & "-" & ((day of dt) as string) & "-" & ((time of dt) as string)
end stamp
"""

# Every value arrives as argv. A lookup error is not caught: it must not read as "no duplicate".
ADD_SCRIPT = """
on run argv
    set theTitle to item 1 of argv
    set startDate to my makeDate(item 2 of argv, item 3 of argv, item 4 of argv, item 5 of argv)
    set endDate to my makeDate(item 6 of argv, item 7 of argv, item 8 of argv, item 9 of argv)
    set calName to item 10 of argv
    set theBody to item 11 of argv
    set theMarker to item 12 of argv
    tell application "Calendar"
        if calName is "" then
            set candidates to (calendars whose writable is true)
        else
            set candidates to (calendars whose name is calName)
        end if
        if (count of candidates) is 0 then return "NOCAL|||"
        if calName is "" and (count of candidates) > 1 then
            set theNames to ""
            repeat with aCal in candidates
                set theNames to theNames & (name of aCal) & linefeed
            end repeat
            return "AMBIGUOUS|||" & theNames
        end if
        set targetCal to item 1 of candidates
        repeat with ev in (events of targetCal whose start date is startDate)
            set theNotes to description of ev
            if theNotes is not missing value then
                if theNotes contains theMarker then return "EXISTS|||" & (uid of ev) & "|||" & (name of targetCal)
            end if
        end repeat
        set newEvent to make new event at end of events of targetCal with properties {summary:theTitle, start date:startDate, end date:endDate, description:theBody}
        try
            return "CREATED|||" & (uid of newEvent) & "|||" & (name of targetCal)
        on error errMsg
            return "CREATED||||||" & errMsg
        end try
    end tell
end run
""" + HANDLERS

READ_BACK_SCRIPT = """
on run argv
    set theUid to item 1 of argv
    set calName to item 2 of argv
    tell application "Calendar"
        repeat with aCal in (calendars whose name is calName)
            set found to (events of aCal whose uid is theUid)
            if (count of found) > 0 then
                set ev to item 1 of found
                return my stamp(start date of ev) & "|||" & my stamp(end date of ev) & "|||" & (name of aCal) & "|||" & (summary of ev)
            end if
        end repeat
        return ""
    end tell
end run
""" + HANDLERS


class WriteError(Exception):
    pass


class WriteTimeout(WriteError):
    pass


def parse_when(text: str) -> datetime:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    raise WriteError(f"cannot read the time {text!r}; use YYYY-MM-DD HH:MM (local time)")


def date_args(dt: datetime) -> list[str]:
    return [str(dt.year), str(dt.month), str(dt.day), str(dt.hour * 3600 + dt.minute * 60 + dt.second)]


def stamp(dt: datetime) -> str:
    return "-".join(date_args(dt))


def event_key(title: str, start: datetime, end: datetime, calendar: str, key: str = "") -> str:
    """The caller's key, else one derived from the request, so a retry names the same event."""
    if key:
        if not KEY_FORMAT.fullmatch(key):
            raise WriteError("--key takes 1-64 letters, digits, dots, dashes or underscores")
        return key
    request = "\x1f".join([title.strip(), stamp(start), stamp(end), calendar.strip().casefold()])
    return hashlib.sha256(request.encode()).hexdigest()[:16]


def marker(key: str) -> str:
    return f"[sutando-key:{key}]"


def may_exist(uid: str = "", calendar: str = "") -> str:
    """The retry warning for any failure at or after the create step."""
    where = f" (uid {uid}, calendar {calendar!r})" if uid else ""
    return f" The event may have been created{where}: check Calendar before retrying."


def osascript_command(script: str, args: list[str]) -> list[str]:
    # `--` ends osascript's own options: without it a value such as "-e …" runs as script text.
    return ["osascript", "-e", script, "--", *args]


@contextlib.contextmanager
def key_lock(key: str):
    """One lookup-and-create at a time per key on this host; WriteError while another run holds it."""
    name = f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.lock"
    try:
        folder = consent.state_dir() / "calendar-writer-locks"
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        folder = Path(tempfile.gettempdir())
    try:
        handle = open(folder / name, "w")
    except OSError as e:
        raise WriteError(f"could not take the lock for this event ({e}); nothing was added")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WriteError("another run is adding this same event right now; nothing was added by "
                             "this one: check Calendar before retrying.")
        yield
    finally:
        handle.close()


def run_script(script: str, args: list[str], unconfirmed: str = "") -> str:
    """Run one AppleScript with argv; stdout without its newline, WriteError on any failure."""
    try:
        result = subprocess.run(osascript_command(script, args),
                                capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise WriteTimeout(consent.timeout_message(APP))
    except OSError as e:
        raise WriteError(f"could not run osascript ({e})")
    if unconfirmed and consent.is_denied(result.stderr):
        print(f"Error:{unconfirmed}", file=sys.stderr)
    consent.exit_if_denied(APP, result.stderr)
    if result.returncode != 0:
        raise WriteError(result.stderr.strip() or "AppleScript error")
    return result.stdout.rstrip("\n")


def add_event(title: str, start: datetime, end: datetime, calendar: str = "", notes: str = "",
              key: str = "") -> dict:
    key = event_key(title, start, end, calendar, key)
    body = f"{notes}\n\n{marker(key)}" if notes else marker(key)
    print(f"Adding to Calendar: {title!r}, {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}, "
          f"calendar {calendar or '(the only writable one)'}"
          f"{', notes ' + repr(notes) if notes else ''}, key {key}", file=sys.stderr)

    # Calendar must be running (-600 otherwise); a failed launch surfaces in the script below.
    try:
        subprocess.run(["open", "-ga", APP], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    time.sleep(1)

    args = [title, *date_args(start), *date_args(end), calendar, body, marker(key)]
    try:
        with key_lock(key):
            answer = run_script(ADD_SCRIPT, args)
    except WriteTimeout as e:
        # A timeout leaves the create step undecided; an AppleScript error means nothing was made.
        raise WriteError(f"{e}{may_exist()}")
    status, _, rest = answer.partition(SEP)
    if status == "NOCAL":
        raise WriteError(f"no calendar named {calendar!r}; nothing was added" if calendar
                         else "no writable calendar; nothing was added")
    if status == "AMBIGUOUS":
        names = ", ".join(repr(n) for n in rest.splitlines() if n.strip())
        raise WriteError(f"several calendars can take the event ({names}); nothing was added. "
                         f"Ask the owner which one and pass --calendar NAME.")
    uid, _, cal_name = rest.partition(SEP)
    uid = uid.strip()
    if status not in ("CREATED", "EXISTS") or not uid:
        raise WriteError(f"Calendar did not return an event id (got {answer!r}).{may_exist()}")
    existed = status == "EXISTS"

    unverified = (f" An event with key {key} already exists (uid {uid}, calendar {cal_name!r}): "
                  f"check it in Calendar." if existed else may_exist(uid, cal_name))
    try:
        read = run_script(READ_BACK_SCRIPT, [uid, cal_name], unconfirmed=unverified)
    except WriteError as e:
        raise WriteError(f"read-back failed: {e}{unverified}")
    parts = read.split(SEP, 3)
    if len(parts) < 4:
        raise WriteError(f"the event was not found on read-back.{unverified}")
    got_start, got_end, got_cal, got_title = parts
    asked_cal = calendar.strip() or cal_name
    got = (got_start, got_end, got_cal.strip().casefold(), got_title.strip())
    if got != (stamp(start), stamp(end), asked_cal.casefold(), title.strip()):
        raise WriteError(
            f"read-back does not match the request: asked {title!r} {stamp(start)} to "
            f"{stamp(end)} in {asked_cal!r}, Calendar has {got_title!r} {got_start} to {got_end} "
            f"in {got_cal!r}. The event exists (uid {uid}): check Calendar before retrying.")
    return {"created": not existed, "already_exists": existed, "uid": uid, "calendar": got_cal,
            "title": title, "start": start.isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"), "key": key}


def _take_option(argv: list[str], name: str) -> str:
    if name not in argv:
        return ""
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise WriteError(f"{name} needs a value")
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


def require_write_consent(argv: list[str]) -> list[str]:
    """The per-run flag, on the owner's own known task; argv without the flag."""
    if consent.FLAG not in argv:
        print(WRITE_NEEDS_FLAG, file=sys.stderr)
        sys.exit(consent.EXIT_NO_CONSENT)
    argv = consent.require_consent(APP, argv)
    # Fail closed inside a core session that names no readable task.
    if consent.in_agent_session() and consent.bound_task_tier() is None:
        print(UNVERIFIED, file=sys.stderr)
        sys.exit(consent.EXIT_NO_CONSENT)
    return argv


def main(argv=None) -> None:
    argv = require_write_consent(list(sys.argv if argv is None else argv))
    consent.exit_if_denied_earlier(APP)
    try:
        calendar = _take_option(argv, "--calendar")
        notes = _take_option(argv, "--notes")
        key = _take_option(argv, "--key")
        if len(argv) != 5 or argv[1] != "add":
            print(USAGE, file=sys.stderr)
            sys.exit(1)
        title, start, end = argv[2], parse_when(argv[3]), parse_when(argv[4])
        if not title.strip():
            raise WriteError("the title is empty")
        if end <= start:
            raise WriteError("the end must be after the start")
        result = add_event(title, start, end, calendar, notes, key)
    except WriteError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
