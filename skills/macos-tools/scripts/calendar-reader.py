#!/usr/bin/env python3
"""
Sutando calendar reader — reads macOS Calendar events via AppleScript.

Usage (``--owner-asked`` only when the owner asked for the local Calendar app —
it raises a macOS permission prompt; without it the script refuses, exit 2):
  python3 calendar-reader.py --owner-asked          # next 7 days (default)
  python3 calendar-reader.py 1 --owner-asked        # today only
  python3 calendar-reader.py 30 text --owner-asked  # next 30 days, plain text

Output: JSON with events sorted by start time. A macOS denial (-1743) exits 3.
"""

import sys
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

# Sibling helper: the scripts run by path, so their directory is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_pim_consent as consent  # noqa: E402


def read_events(days: int = 7) -> dict:
    # Ensure Calendar.app is running (AppleScript fails with -600 if not)
    subprocess.run(["open", "-ga", "Calendar"], capture_output=True, timeout=5)
    time.sleep(1)
    script = f"""
tell application "Calendar"
    set output to ""
    set startDate to current date
    set endDate to startDate + ({days} * days)

    repeat with aCal in calendars
        set calName to name of aCal
        set theEvents to (events of aCal whose start date >= startDate and start date <= endDate)
        repeat with anEvent in theEvents
            set evtTitle to summary of anEvent
            set evtStart to start date of anEvent
            set evtEnd to end date of anEvent
            try
                set evtLocation to location of anEvent
            on error
                set evtLocation to ""
            end try
            try
                set evtNotes to description of anEvent
            on error
                set evtNotes to ""
            end try
            set allDay to allday event of anEvent
            set output to output & calName & "|||" & evtTitle & "|||" & (evtStart as string) & "|||" & (evtEnd as string) & "|||" & evtLocation & "|||" & evtNotes & "|||" & (allDay as string) & "\\n"
        end repeat
    end repeat
    return output
end tell
"""

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Calendar read timed out", "events": [], "count": 0}

    if result.returncode != 0:
        err = result.stderr.strip()
        if consent.is_denied(err):
            return {"error": consent.denial_message("Calendar"), "denied": True,
                    "events": [], "count": 0}
        # Calendar access denied
        if "not allowed" in err.lower() or "authorization" in err.lower():
            return {"error": "Calendar access denied — grant access in System Settings → Privacy → Calendars", "events": [], "count": 0}
        return {"error": err or "AppleScript error", "events": [], "count": 0}

    events = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|||")
        if len(parts) < 4:
            continue
        location = parts[4].strip() if len(parts) > 4 else ""
        notes = parts[5].strip()[:300] if len(parts) > 5 else ""
        events.append({
            "calendar": parts[0].strip(),
            "title": parts[1].strip(),
            "start": parts[2].strip(),
            "end": parts[3].strip(),
            "location": "" if location in ("missing value", "missing value") else location,
            "notes": "" if notes == "missing value" else notes,
            "all_day": parts[6].strip().lower() == "true" if len(parts) > 6 else False,
        })

    # Sort by raw AppleScript date string — format: "Sunday, March 16, 2026 at 9:00:00 AM"
    # Use the order they come (Calendar already sorts within each calendar)
    # Secondary sort by title for stability
    events.sort(key=lambda e: (e["start"], e["title"]))

    return {"events": events, "count": len(events), "days": days}


def format_for_humans(data: dict) -> str:
    """Plain-text summary suitable for voice or briefing context."""
    if data.get("error"):
        return f"Calendar error: {data['error']}"
    events = data.get("events", [])
    if not events:
        return f"No events in the next {data.get('days', 7)} days."

    lines = [f"Next {data.get('days', 7)} days ({len(events)} events):"]
    for e in events:
        time_str = e["start"]
        if e["all_day"]:
            time_str = time_str.split(" at ")[0] + " (all day)"
        line = f"  [{e['calendar']}] {e['title']} — {time_str}"
        if e["location"]:
            line += f" @ {e['location']}"
        lines.append(line)
    return "\n".join(lines)


def main(argv=None) -> None:
    argv = consent.require_consent("Calendar", argv)
    days = int(argv[1]) if len(argv) > 1 else 7
    fmt = argv[2] if len(argv) > 2 else "json"

    data = read_events(days)
    if data.get("denied"):
        print(data["error"], file=sys.stderr)
        sys.exit(consent.EXIT_DENIED)

    if fmt == "text":
        print(format_for_humans(data))
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
