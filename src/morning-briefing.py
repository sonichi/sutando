#!/usr/bin/env python3
"""Morning briefing for Sutando.

Runs daily at 6:57am via cron. No external credentials needed.
Sources: weather (Open-Meteo), macOS Calendar, macOS Reminders,
overnight Discord DMs, pending questions, system health.

Output: results/proactive-<ts>.txt (voice speaks it) + Discord DM.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).parent))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
RESULTS_DIR = WORKSPACE / "results"
LOGS_DIR = WORKSPACE / "logs"
STATE_DIR = WORKSPACE / "state"

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


def _run_applescript(script: str, timeout: int = 8) -> str | None:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def get_weather() -> str:
    """Fetch current conditions from Open-Meteo (no key needed)."""
    try:
        # Default to SF; override via TZ if possible
        lat, lon = 37.77, -122.42
        tz_result = _run_applescript(
            'do shell script "defaults read /Library/Preferences/com.apple.timezone"',
            timeout=3
        )
        # Use lat/lon from env if set
        import os
        if os.environ.get("WEATHER_LAT") and os.environ.get("WEATHER_LON"):
            lat = float(os.environ["WEATHER_LAT"])
            lon = float(os.environ["WEATHER_LON"])

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


def get_calendar_events() -> list[dict]:
    """Get today's calendar events via AppleScript."""
    today = datetime.now().strftime("%Y-%m-%d")
    script = f'''
set theDate to date "{today}"
set endDate to theDate + (24 * 60 * 60)
set output to ""
tell application "Calendar"
    repeat with cal in every calendar
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
            set output to output & h & ":" & mStr & ampm & " " & evTitle & "\\n"
        end repeat
    end repeat
end tell
return output
'''
    result = _run_applescript(script, timeout=10)
    if not result:
        return []
    events = []
    for line in result.splitlines():
        line = line.strip()
        if line:
            events.append({"raw": line})
    return events


def get_reminders() -> list[str]:
    """Get today's and overdue reminders via the existing script."""
    script_path = Path(__file__).parent.parent / "skills" / "macos-tools" / "scripts" / "reminders.py"
    if not script_path.exists():
        return []
    try:
        r = subprocess.run(
            [sys.executable, str(script_path), "list", "--due-today"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return []
        items = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
        return items[:5]
    except (subprocess.TimeoutExpired, OSError):
        return []


def get_overnight_discord() -> list[str]:
    """Read last 8 hours of Discord DMs from the bridge log."""
    log = LOGS_DIR / "discord-bridge.log"
    if not log.exists():
        return []
    try:
        cutoff = time.time() - 8 * 3600
        messages = []
        for line in log.read_text(errors="replace").splitlines()[-200:]:
            # Look for DM lines: [msg] #DM @user: text
            if "[msg] #DM" in line and "is_dm: True" in line:
                # Extract sender and preview
                m = re.search(r'\[msg\] #DM @(\S+): (.+?) \(mentions:', line)
                if m:
                    sender, text = m.group(1), m.group(2)[:80]
                    if sender != "Sutando" and "Sutando-Pro" not in sender:
                        messages.append(f"{sender}: {text}")
        return messages[-5:] if messages else []
    except OSError:
        return []


def get_pending_questions() -> list[str]:
    """Return unanswered questions from pending-questions.md."""
    pq = WORKSPACE / "pending-questions.md"
    if not pq.exists():
        return []
    content = pq.read_text()
    questions = []
    for section in re.split(r'^## ', content, flags=re.MULTILINE)[1:]:
        title = section.partition('\n')[0].strip()
        # Strip leading date prefix like "[2026-05-27] "
        title = re.sub(r'^\[\d{4}-\d{2}-\d{2}\]\s*', '', title)
        if title:
            questions.append(title[:60])
    return questions


def get_health_issues() -> list[str]:
    """Run health check and return only the failed/warn items, concisely."""
    hc = Path(__file__).parent / "health-check.py"
    if not hc.exists():
        return []
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
        return issues[:3]
    except (subprocess.TimeoutExpired, OSError):
        return []


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

    # Calendar
    if events:
        count = len(events)
        if count == 1:
            parts.append(f"One meeting today: {events[0]['raw']}.")
        else:
            next_ev = events[0]["raw"]
            parts.append(f"{count} meetings today. First up: {next_ev}.")
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

    # Closing
    if not events and not reminders and not pending_qs and not health_issues:
        parts.append("Everything looks clean. Good day for deep work.")

    return " ".join(parts)


def main():
    # Check sentinel — don't repeat if already run today
    today = datetime.now().strftime("%Y-%m-%d")
    sentinel = STATE_DIR / f"morning-briefing-{today}.sentinel"
    if sentinel.exists() and "--force" not in sys.argv:
        print(f"Morning briefing already delivered today ({today}). Use --force to re-run.")
        return

    print("Gathering morning briefing...")

    # Gather all sources (skip errors silently)
    weather = get_weather()
    print(f"  weather: {weather or 'unavailable'}")

    events = get_calendar_events()
    print(f"  calendar: {len(events)} events")

    reminders = get_reminders()
    print(f"  reminders: {len(reminders)} due")

    discord_msgs = get_overnight_discord()
    print(f"  discord overnight: {len(discord_msgs)} messages")

    pending_qs = get_pending_questions()
    print(f"  pending questions: {len(pending_qs)}")

    health_issues = get_health_issues()
    print(f"  health issues: {len(health_issues)}")

    # Synthesize
    narrative = synthesize(weather, events, reminders, discord_msgs, pending_qs, health_issues)

    # Write voice result
    ts = int(time.time() * 1000)
    result_file = RESULTS_DIR / f"proactive-morning-{ts}.txt"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_file.write_text(narrative)
    print(f"  → {result_file.name}")

    # Mark as done today
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(datetime.now().isoformat())

    print(f"\nBriefing delivered:\n{narrative}")


if __name__ == "__main__":
    main()
