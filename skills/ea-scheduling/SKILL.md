# EA Scheduling

Sutando acts as executive assistant for inbound meeting requests. Finds emails, checks your calendar, drafts availability replies — all autonomously.

**Usage**: `/ea-scheduling` or run directly:
```bash
python3 skills/ea-scheduling/scripts/handle-meeting-requests.py          # dry-run
python3 skills/ea-scheduling/scripts/handle-meeting-requests.py --send   # live
python3 skills/ea-scheduling/scripts/handle-meeting-requests.py --days 14 --limit 10
```

## What it does

1. **Detect** — scans Gmail inbox for unread meeting request emails (regex patterns covering "schedule a call", "find time", "are you available", Calendly links, etc.)
2. **Check** — reads Google Calendar for the next N days to find free 30-min slots during working hours (9 AM–6 PM Dubai)
3. **Draft** — generates a reply with 3 available options in the sender's readable format
4. **Send** — replies to the thread and applies the `EA-Scheduled` Gmail label (only with `--send`)
5. **Dedup** — tracks handled message IDs in `state/ea-handled-ids.json` to avoid double-replies

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--send` | off (dry-run) | Actually send replies and label threads |
| `--days N` | 7 | Days ahead to search for availability |
| `--limit N` | 5 | Max requests to process per pass |

## Config

- **Handled state**: `skills/ea-scheduling/state/ea-handled-ids.json`
- **Label applied**: `EA-Scheduled` (auto-created in Gmail if missing)
- **Slot duration**: 30 min (edit `SLOT_DURATION` in script)
- **Working hours**: 9 AM–6 PM Dubai (UTC+4) — edit `WORK_START`/`WORK_END`

## Use case

Target: senior exec or partner where scheduling logistics eat significant time.

Flow: contact emails Bassil → Sutando replies with 3 slots → contact picks one → Bassil gets a clean booked meeting, never touched the thread.

Demo angle: "Work tried to get on my calendar while I was walking. Sutando handled it."
