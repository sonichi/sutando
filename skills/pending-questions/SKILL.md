---
name: pending-questions
description: Ask the owner a decision you need, list what is waiting on him, close a question once he has answered, or run the reminder. Use when blocked on the owner's word, or asked "what are you waiting on me for?".
---

# pending-questions

One CLI over the owner's pending questions, and the one store behind them: the
"Pending questions" database in the owner's room. Each question is a row carrying its
Host; a host reads and writes only its own rows, and the owner sets a row's Status
himself (code never writes it — its own marks live in the Recovery and Closed cells).

The room is `PENDING_QUESTIONS_ROOM`: env, then this manifest's `config`, then the host's
`<workspace>/state/pending-questions-room`; unset, the owner's DM. Point every host of the
owner at one room shared by him and all his Sutandos.

```bash
python3 skills/pending-questions/scripts/pq.py ask "<question>" [--context "<why / options>"] \
    [--task-file <workspace>/tasks/<task>.txt] [--urgency live|durable] \
    [--default-action "<what Approve does>" --reason "<why>"] [--option "Hold=wait"] \
    [--priority High|Medium|Low]
python3 skills/pending-questions/scripts/pq.py list [--json]
python3 skills/pending-questions/scripts/pq.py resolve <ask-id> [--answered]
python3 skills/pending-questions/scripts/pq.py remind [--force]
```

- `ask` queues the question to the owner (the task's conversation only for an owner-tier
  task in his DM; otherwise his DM), saves it to the outbox, writes the row born with its
  `**Sent:**` record, and deletes the outbox file once the row is confirmed complete. Read
  its output: a `FAILED` line is not an ask. Then continue; never block.
- `list` runs the pass (below) and prints every open row of this host, each with its ask id,
  plus any held question once, marked `(not yet in the room)`.
- `resolve` closes a row as Resolved (or Answered with `--answered`). It never reopens a closed row.
- `remind` runs `src/check-pending-questions.py --notify` with this skill's adapter: the
  only way a reminder is sent. Nothing is scheduled; the owner is asked as questions come up.

## The outbox

`<workspace>/state/pending-questions-outbox/<ask_id>.json` holds a question while the room
is unreachable: one file per ask, written atomically before the room write, never edited,
deleted only when the complete row is confirmed. Every pass — `pq.py list`, `pq.py remind`,
`src/check-pending-questions.py`, the next `ask` — replays it through the normal add_row
path (which resumes a row left incomplete), so a replay is idempotent by ask id. Without
the room-collab capability or an owner room, every ask stays in the outbox and `list`
says why.

## Who reads it

This skill's adapter, `scripts/pending_questions_room_db.py`, is the single reader and
writer (`room_store`, `gather`, `waiting`, `count`, `resolve`); the manifest's
`pending_questions_store` field declares it (`skills/MANIFEST.md`). Core — the dashboard,
the morning briefing, agent-api, friction-detector, session-handoff, the reminder — reaches
it only through `src/pending_questions_reader.py`, by path, and reads no file.

## Migration from the per-host file

- **Existing `<workspace>/hosts/<host>/pending-questions.md` files** are read-only history.
  The one transitional reader, `src/pending_questions_compat.ingest_legacy_file_entries`
  (called from every pass), copies each open entry carrying an `**Ask id:**` line into this
  host's row once and marks the entry `**Status:** moved — as row q-…` in place; entries
  without an ask id are history and are not surfaced.
- **Rolling upgrade.** While an old head still writes the file, the new head's next pass
  ingests those entries; nothing is lost in the window. The ingest is removed under
  `docs/migration-transition-window.md`: ~30 days of zero source-side writes, checked with
  `python3 src/pending_questions_compat.py report` (the last mutation of each host's legacy
  file; the clock starts at the newest).
- **Persisted schedules.** The `pending-questions` entry is gone from
  `skills/schedule-crons/crons.example.json`. An old `crons.json` entry that still runs
  `python3 src/check-pending-questions.py` (with no flag or the retired `--reconcile-only`)
  now reconciles and lists; it sends nothing.

## Testing against an unreachable room

`PENDING_QUESTIONS_COLLAB_URL=<url>` points the adapter's collab service at that URL for a
run, so an outage can be rehearsed: `ask` leaves the question in the outbox, and a later
`list` with the variable unset files it.
