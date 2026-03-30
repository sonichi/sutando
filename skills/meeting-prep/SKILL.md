---
name: meeting-prep
description: "Auto-prepare for upcoming meetings: attendee info, recent email threads, talking points, and agenda. Runs 30 min before each meeting or on demand."
user-invocable: true
---

# Meeting Prep

Prepare a briefing for an upcoming meeting — attendee info, recent context, and talking points.

**Usage**: `/meeting-prep [meeting name or time]`

ARGUMENTS: $ARGUMENTS

## How it works

1. **Find the meeting.** If ARGUMENTS specifies a meeting name or time, find it. Otherwise, find the next meeting starting within 60 minutes.

```bash
~/.claude/skills/google-calendar/scripts/google-calendar.py events list \
  --time-min NOW --time-max NOW_PLUS_60MIN
```

2. **Extract attendees.** From the calendar event, get the list of attendee emails.

3. **Look up each attendee.** For each attendee (skip the owner):

   a. **Contacts** — search by email:
   ```bash
   python3 ~/.claude/skills/macos-tools/scripts/contacts.py search "email@example.com"
   ```

   b. **Recent emails** — search Gmail for recent threads with this person:
   ```bash
   gws gmail users messages list --params 'q=from:email@example.com OR to:email@example.com newer_than:14d'
   ```
   Read the top 2-3 threads to extract context.

   c. **Web presence** — if the person is external or unfamiliar, do a quick web search for their name + company to understand their role.

4. **Build the brief.** Generate a concise prep document:

```
Meeting: [title]
Time: [start] - [end]
Location: [link or room]

Attendees:
- [Name] ([role/company]) — [1-line context from recent emails]
- ...

Recent context:
- [Key thread 1 summary]
- [Key thread 2 summary]

Suggested talking points:
- [Based on recent threads and meeting title]
- ...

Action items to follow up on:
- [Any commitments from prior meetings/emails]
```

5. **Deliver.** Write to `results/meeting-prep-{timestamp}.txt` so the voice agent can speak it. Also write to `notes/meeting-prep-{date}-{title-slug}.md` for reference.

## Auto-scheduling

The proactive loop should check for meetings starting in the next 30-45 minutes. If one is found and no prep exists yet, run this skill automatically. Add this check to the proactive loop:

```
Check calendar for meetings in next 30-45 min.
If found and no notes/meeting-prep-{date}-{slug}.md exists, run /meeting-prep.
```

## Tips

- Skip recurring 1:1s unless the attendee is new or there are recent email threads
- For large meetings (>5 attendees), focus on the organizer and key participants
- If the meeting has an agenda doc linked, read and summarize it
- Keep the brief voice-friendly — it will be spoken aloud
