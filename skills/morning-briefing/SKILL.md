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

Collect from each source (skip any that aren't configured):

1. **Email** — Run `gws gmail +triage` to get unread inbox. Summarize top 5 by priority. Flag anything urgent.

2. **Calendar** — Run `~/.claude/skills/google-calendar/scripts/google-calendar.py events list --time-min TODAY_START --time-max TODAY_END`. List meetings with times. For each: who's attending, what it's about.

3. **Discord** — Fetch recent messages from configured channels (reference_discord_channels.md). Summarize anything actionable from overnight.

4. **Pending tasks** — Check `pending-questions.md` for unanswered items. Check `tasks/` for queued tasks.

5. **System status** — Run `python3 src/health-check.py`. Report any issues.

6. **Daily insight** — Run `python3 src/daily-insight.py`. If it produces an insight, include it at the end of the briefing as "💡 Insight: ..."

7. **Friction check** — Run `python3 src/friction-detector.py`. If friction items found, include as "⚠️ Friction: [count] items need attention" with the top 3.

## How to deliver

Format as a concise briefing:

```
Good morning. Here's your briefing:

📧 Email: [count] unread. [urgent summary]
📅 Calendar: [count] meetings today. [next meeting info]
💬 Discord: [summary of overnight activity]
📋 Tasks: [pending items]
🖥️ System: [health status]
💡 Insight: [behavioral pattern from daily-insight.py, if available]
```

Deliver via:
- Write to `results/briefing-{date}.txt` so the voice agent can speak it
- Send via Discord DM if configured

## Scheduling

To run daily, add to the proactive loop or use `/loop`:
```
/loop 24h /morning-briefing
```

Or schedule at a specific time via cron.
