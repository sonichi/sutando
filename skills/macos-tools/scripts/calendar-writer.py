#!/usr/bin/env python3
"""
Sutando calendar writer — adds an event to the local macOS Calendar via AppleScript.

Usage (``--owner-asked`` only when the owner asked for the local Calendar app —
it raises a macOS permission prompt; without it the script refuses, exit 2):
  python3 calendar-writer.py add "Dentist" "2026-03-17 14:00" "2026-03-17 15:00" --owner-asked
  python3 calendar-writer.py add "Standup" 2026-03-17T09:00 2026-03-17T09:15 \\
      --calendar Work --notes "Room 4" --owner-asked

Times are local, ``YYYY-MM-DD HH:MM`` (or ``T`` between). Without ``--calendar`` the
event goes to the first writable calendar. The event is read back by uid: JSON with
``created: true`` and exit 0 only when the read-back matches the request. A macOS
denial (-1743) exits 3; every other failure exits 1 with the cause on stderr.
"""
from __future__ import annotations

import json
import subprocess
import sys
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
USAGE = ('Usage: python3 calendar-writer.py add "<title>" "<YYYY-MM-DD HH:MM>" "<YYYY-MM-DD HH:MM>" '
         "[--calendar NAME] [--notes TEXT] --owner-asked")

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

# Every value arrives as argv, and the event is made in one step: no later step can half-fail.
ADD_SCRIPT = """
on run argv
    set theTitle to item 1 of argv
    set startDate to my makeDate(item 2 of argv, item 3 of argv, item 4 of argv, item 5 of argv)
    set endDate to my makeDate(item 6 of argv, item 7 of argv, item 8 of argv, item 9 of argv)
    set calName to item 10 of argv
    set theNotes to item 11 of argv
    tell application "Calendar"
        if calName is "" then
            set targetCal to first calendar whose writable is true
        else
            set targetCal to first calendar whose name is calName
        end if
        if theNotes is "" then
            set newEvent to make new event at end of events of targetCal with properties {summary:theTitle, start date:startDate, end date:endDate}
        else
            set newEvent to make new event at end of events of targetCal with properties {summary:theTitle, start date:startDate, end date:endDate, description:theNotes}
        end if
        try
            return (uid of newEvent) & "|||" & (name of targetCal)
        on error errMsg
            return "|||" & errMsg
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
                return my stamp(start date of ev) & "|||" & my stamp(end date of ev) & "|||" & (summary of ev)
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


def may_exist(uid: str = "", calendar: str = "") -> str:
    """The retry warning for any failure at or after the create step."""
    where = f" (uid {uid}, calendar {calendar!r})" if uid else ""
    return f" The event may have been created{where}: check Calendar before retrying."


def run_script(script: str, args: list[str], unconfirmed: str = "") -> str:
    """Run one AppleScript with argv; stdout without its newline, WriteError on any failure."""
    try:
        result = subprocess.run(["osascript", "-e", script, *args],
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


def add_event(title: str, start: datetime, end: datetime, calendar: str = "", notes: str = "") -> dict:
    # Calendar must be running (-600 otherwise); a failed launch surfaces in the script below.
    try:
        subprocess.run(["open", "-ga", APP], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    time.sleep(1)

    args = [title, *date_args(start), *date_args(end), calendar, notes]
    try:
        created = run_script(ADD_SCRIPT, args)
    except WriteTimeout as e:
        # A timeout leaves the create step undecided; an AppleScript error means nothing was made.
        raise WriteError(f"{e}{may_exist()}")
    uid, _, cal_name = created.partition(SEP)
    uid = uid.strip()
    if not uid:
        raise WriteError(f"Calendar did not return an event id (got {created!r}).{may_exist()}")

    unverified = may_exist(uid, cal_name)
    try:
        read = run_script(READ_BACK_SCRIPT, [uid, cal_name], unconfirmed=unverified)
    except WriteError as e:
        raise WriteError(f"read-back failed: {e}{unverified}")
    parts = read.split(SEP, 2)
    if len(parts) < 3:
        raise WriteError(f"the new event was not found on read-back.{unverified}")
    got_start, got_end, got_title = parts
    if (got_start, got_end, got_title.strip()) != (stamp(start), stamp(end), title.strip()):
        raise WriteError(
            f"read-back does not match the request: asked {title!r} {stamp(start)} to "
            f"{stamp(end)}, Calendar has {got_title!r} {got_start} to {got_end}. The event exists "
            f"(uid {uid}, calendar {cal_name!r}): check Calendar before retrying.")
    return {"created": True, "uid": uid, "calendar": cal_name, "title": title,
            "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}


def _take_option(argv: list[str], name: str) -> str:
    if name not in argv:
        return ""
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise WriteError(f"{name} needs a value")
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


def main(argv=None) -> None:
    argv = consent.require_consent(APP, argv)
    consent.exit_if_denied_earlier(APP)
    try:
        calendar = _take_option(argv, "--calendar")
        notes = _take_option(argv, "--notes")
        if len(argv) != 5 or argv[1] != "add":
            print(USAGE, file=sys.stderr)
            sys.exit(1)
        title, start, end = argv[2], parse_when(argv[3]), parse_when(argv[4])
        if not title.strip():
            raise WriteError("the title is empty")
        if end <= start:
            raise WriteError("the end must be after the start")
        result = add_event(title, start, end, calendar, notes)
    except WriteError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
