---
name: meeting-scheduler
description: "Schedule a small meeting end-to-end: resolve attendee emails, check the owner's calendar for the slot, dedup-check, then create + email the Google Calendar invite. The mechanical core only — cross-person availability negotiation stays interactive."
user-invocable: true
---

# Meeting Scheduler

Encapsulates the ~10 manual steps of "set up a quick meeting" into one command:
**resolve attendee emails → check the owner's calendar for the slot → dedup-check →
create the event + send invites → report the link.**

**Usage**: `/meeting-scheduler [what to schedule]`

ARGUMENTS: $ARGUMENTS

## Scope (read first)

This skill handles the **mechanical** core only. It does **NOT** negotiate
availability across people — proposing times, collecting "does 3pm work?",
juggling everyone's free/busy stays an **interactive** conversation you have with
the owner (and, if asked, the attendees). Once a concrete slot is chosen, this
skill does the rote work.

**Authority note.** Creating and sending a calendar invite is an **owner action**.
Run the create+send step only when the owner has invoked this skill for a concrete
meeting. Never auto-send speculatively — default to a dry-run and show the plan
first. The helper enforces this: it defaults to `--dry-run` and only creates with
an explicit `--send`.

## Inputs

| Input | Required | Notes |
|---|---|---|
| title | yes | Event summary, e.g. "Sutando sync" |
| attendees | yes | Either explicit emails (`--attendees`) or names to resolve (`--resolve`) |
| when | yes | Start time — ISO 8601 (`2026-07-25T15:00`) or `today/tomorrow HH[:MM][am/pm]` |
| duration | no | Minutes; default 30 |
| location | no | Room, address, or a video link |
| description | no | Agenda / notes |

Emails must come from a **Gmail lookup** — the identity map
(`workspace/.claude-sutando/projects/<slug>/memory/reference_identity_map.md`)
resolves a name to a *person* (and their Discord/GitHub ids) but does **not**
store email addresses. Use the map to disambiguate *who* is meant; get the address
from Gmail.

## Run order

Everything below is done by `scripts/schedule_meeting.py`, which shells out to the
`gws` (Google Workspace) CLI. You can run it directly, or perform the steps by
hand with the `gws` commands shown.

### 1. Resolve attendee emails (names → emails via Gmail)

For each name, search recent mail and read From/To headers to extract the address:

```bash
gws gmail users messages list --params '{"userId":"me","q":"\"Alice\" newer_than:365d","maxResults":10}'
# then, per hit id:
gws gmail users messages get --params '{"userId":"me","id":"<id>","format":"metadata","metadataHeaders":["From","To","Cc"]}'
```

Strip any line containing `keyring` from gws output before parsing JSON. For the
**ag2.ai** account, set `GOOGLE_WORKSPACE_CLI_CONFIG_DIR=$HOME/.config/gws-ag2`
(the helper's `--account ag2` does this). If a name is ambiguous, confirm with the
owner rather than guessing.

### 2. Check the OWNER's calendar for the slot

Read the owner's events across that day and flag anything whose busy time overlaps
`[start, end)` (back-to-back is *not* a conflict; `transparency:transparent` /
all-day events don't block):

```bash
gws calendar events list --params '{"calendarId":"primary","timeMin":"2026-07-25T00:00:00-07:00","timeMax":"2026-07-26T00:00:00-07:00","singleEvents":true,"orderBy":"startTime"}'
```

### 3. Dedup-check

From the same day's events, refuse if one already has the same title
(case/space-insensitive, ignoring cancelled ones) — prevents a double-booked
duplicate invite.

### 4. Create the event + send invites

Only on explicit owner action (`--send`). `sendUpdates=all` makes Google email the
invitations:

```bash
gws calendar events insert \
  --params '{"calendarId":"primary","sendUpdates":"all"}' \
  --json '{"summary":"Sutando sync","location":"Meet link","description":"...","start":{"dateTime":"2026-07-25T15:00:00","timeZone":"America/Los_Angeles"},"end":{"dateTime":"2026-07-25T15:30:00","timeZone":"America/Los_Angeles"},"attendees":[{"email":"a@x.com"},{"email":"b@y.com"}]}'
```

### 5. Report the event link

Print the `htmlLink` from the insert response back to the owner.

## The helper script

```bash
# Dry-run (default) — resolve, check, dedup, report. NO changes:
python3 skills/meeting-scheduler/scripts/schedule_meeting.py \
  --title "Sutando sync" --when 2026-07-25T15:00 --duration-min 30 \
  --resolve "Alice, Bob"

# Create + email invites (owner action):
python3 skills/meeting-scheduler/scripts/schedule_meeting.py \
  --title "Sutando sync" --when 2026-07-25T15:00 --duration-min 30 \
  --attendees "a@x.com,b@y.com" --location "https://meet.google.com/xxx" --send

# Override a detected conflict/duplicate (owner's explicit call):
#   ... --send --force
```

Flags: `--title --when --duration-min --attendees --resolve --location
--description --calendar (default primary) --timezone --account {default,ag2}
--gmail-lookback --send/--dry-run --force --self-check --verbose`.

**Fail-safe behavior:**
- Defaults to `--dry-run` — nothing is created or emailed without `--send`.
- On a **conflict** or a same-title **duplicate**, it refuses to create even with
  `--send`, unless `--force` is also given (the owner's explicit override).
- If no attendee email is present (none given, none resolved), it stops.

## Offline self-check

The pure logic (when-parsing, conflict overlap, dedup, email-pick) is unit-tested
without the network:

```bash
python3 skills/meeting-scheduler/scripts/schedule_meeting.py --self-check
python3 tests/meeting-scheduler.test.py
```

## Notes / limitations

- **Natural-language dates are intentionally minimal** — ISO 8601 and
  `today/tomorrow HH[:MM][am/pm]` only. For "next Tuesday afternoon" etc., resolve
  it to a concrete ISO time (the owner picks the slot) before calling the helper.
- Free/busy is checked on the **owner's** calendar only — attendee availability is
  the interactive part this skill deliberately leaves out.
- Timezone defaults to the host's IANA zone (falls back to `America/Los_Angeles`);
  override with `--timezone`.
