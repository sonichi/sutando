---
name: morning-briefing
description: "Generate a daily morning briefing: email, calendar, Discord, and news — delivered via voice or Discord DM."
user-invocable: true
---

# Morning Briefing

Generate a prioritized daily briefing from all your channels.

**Usage**: `/morning-briefing`

ARGUMENTS: $ARGUMENTS

## What to gather

**Step 0 — Base data (canonical, always run first):**

```bash
WORKSPACE="$(bash scripts/sutando-config.sh workspace)"
python3 src/morning-briefing.py
```

`src/morning-briefing.py` is the single source of truth for core briefing data: weather (Open-Meteo), macOS Calendar, macOS Reminders, overnight Discord DMs, pending questions, and system health. It writes output to `results/proactive-<ts>.txt` and sends a Discord DM directly. Review its output before composing the full briefing — do NOT re-fetch those sources manually.

**Then augment with the following if configured (skip if not available):**

1. **Email** — Run `gws gmail +triage` to get unread inbox. Summarize top 5 by priority. Flag anything urgent.

2. **GWS Calendar** — If the user uses Google Calendar (not just macOS Calendar), run `gws calendar +agenda --today`. List any meetings not already covered by the macOS Calendar output above.

3. **Daily insight** — Run `python3 src/daily-insight.py`. If it produces an insight, include it at the end of the briefing as "💡 Insight: ..."

4. **Friction check** — Run `python3 src/friction-detector.py`. If friction items found, include as "⚠️ Friction: [count] items need attention" with the top 3.

## How to deliver

`src/morning-briefing.py` already writes `results/proactive-<ts>.txt` (spoken by voice) and sends a Discord DM for the base data. If you gathered email or insight in steps 1–4, append them as a follow-up proactive file:

```bash
echo "📧 Email: [count] unread. [summary]
💡 Insight: [insight text]" > "$WORKSPACE/results/proactive-$(date +%s).txt"
```

## Calendar source (Google Workspace) — activation

`src/morning-briefing.py` is a standalone script and **cannot reach the owner's Google Workspace calendar** — the Station/Composio connector is agent-only. So the briefing reads a cache that the *agent* produces:

- **Producer:** `src/write_calendar_cache.py` writes `state/calendar-today.json` — `{"date": "YYYY-MM-DD", "events": [{"raw": "...", "calendar": "..."}]}`, atomically (tmp + `os.replace`). `date` is today in local time so a stale cache is ignored, and `events: []` means a *verified-empty* day (never rendered as "clear" from a missing cache). Feed it the events you pulled from the connector:
  ```bash
  echo '[{"raw":"9:00-9:30am 1:1 w/ Sam","calendar":"work"}]' | python3 src/write_calendar_cache.py
  python3 src/write_calendar_cache.py --empty   # verified no events today
  ```
- **Reader:** `get_calendar_events()` prefers the cache. Set `MORNING_BRIEFING_CALENDAR_SOURCE=google` to make the cache the *only trusted source* — if it's missing/stale the briefing reports "couldn't read your calendar" rather than falling back to a local macOS Calendar that may not include the work account (the 2026-07-21 "falsely clear" bug, #2256).
- **Without a Google connector:** skip the producer step and the env var; the reader falls back to local macOS Calendar via AppleScript.

The producer must be **invoked by the schedule** (below) — nothing writes the cache automatically, so a morning-briefing cron that only runs the reader will report unread on a `google`-source host.

## Scheduling

The canonical daily schedule produces the Google-calendar cache first, then runs the briefing against it (see the activation section above):

```json
{
  "name": "morning-briefing",
  "cron": "57 6 * * *",
  "prompt": "Morning briefing. FIRST produce the calendar cache from the owner's REAL Google calendar (the standalone script can't reach the connector): pull today's events via the Google-calendar connector (e.g. sutando-station composio_exec GOOGLECALENDAR_EVENTS_LIST, calendarId=primary, today's local-day window), then pipe them as a JSON array of {raw,calendar} to `python3 src/write_calendar_cache.py` (or `--empty` if genuinely no events). THEN run `MORNING_BRIEFING_CALENDAR_SOURCE=google python3 src/morning-briefing.py` to deliver the briefing. Speak the result if voice is connected, send as Discord DM otherwise."
}
```

Calling `/morning-briefing` manually runs the same script plus GWS/insight augmentation.
