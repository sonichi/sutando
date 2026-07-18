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

## Scheduling

The canonical daily schedule calls the script directly (same code path):

```json
{
  "name": "morning-briefing",
  "cron": "57 6 * * *",
  "prompt": "Run python3 src/morning-briefing.py to deliver the daily morning briefing (weather, calendar, reminders, overnight Discord, pending questions, health). Speak the result if voice is connected, send as Discord DM otherwise."
}
```

Calling `/morning-briefing` manually runs the same script plus GWS/insight augmentation.
