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
owner at one room shared by him and all his Sutandos. The collab service is the room
capability's own, unless `PENDING_QUESTIONS_COLLAB_URL` names one (env, then this manifest's
`config`, per `skills/MANIFEST.md`); the adapter's `room_store(..., collab_url=)` is the CLI
tier above both.

```bash
python3 skills/pending-questions/scripts/pq.py ask "<question>" [--context "<why / options>"] \
    [--task-file <workspace>/tasks/<task>.txt] [--urgency live|durable] \
    [--default-action "<what Approve does>" --reason "<why>"] [--option "Hold=wait"] \
    [--priority High|Medium|Low]
python3 skills/pending-questions/scripts/pq.py list [--json]
python3 skills/pending-questions/scripts/pq.py reconcile
python3 skills/pending-questions/scripts/pq.py resolve <ask-id> [--answered]
python3 skills/pending-questions/scripts/pq.py remind [--force]
```

- `ask` reconciles, queues the question to the owner (the task's conversation only for an
  owner-tier task in his DM; otherwise his DM), saves it to the outbox, writes the row born
  with its `**Sent:**` record, and once that exact row is confirmed complete commits the
  store-history marker and THEN deletes the outbox file — a marker that cannot be written
  keeps the file (`history: FAILED` in the report) so the local evidence outlives the
  failure. If the outbox cannot hold the question, NO row is written: the owner is still
  asked, and the report says nothing holds it. Read the output: a `FAILED` line is not an
  ask. Then continue; never block.
- `list` is READ-ONLY — it writes no file, not even the history marker — and lists each ask
  id in exactly one bucket, by precedence: a terminal or locally closed row is done; a
  complete open row waits; an open row whose body has not landed is stood in for by its held
  entry; a held entry with no row waits, marked `(not yet in the room)`, or is done when
  closed locally; a local close naming neither is `closed locally; its row is not in view
  yet` (neither open nor done, `pending_close` in `count`). When the room cannot be read it
  prints `pending questions: UNKNOWN — room unreachable (…)` and only what is held locally;
  it never prints a zero it did not measure. Once a row of this workspace was ever confirmed
  (written, replayed, ingested or observed by a reconcile), a room that cannot be reached is
  an outage, not an empty outbox — whatever became of the one-time introduction message.
- `reconcile` is the explicit pass: outbox replay, local close records, stale marks, the
  transitional legacy ingest, and the history backfill for rows that predate the marker.
  Every step commits the history marker before it releases local evidence (an outbox entry,
  a close record, a legacy file entry), and keeps that evidence when the commit fails. `ask`
  and `remind` run it; `list` does not.
- `resolve` closes a row as Resolved (or Answered with `--answered`). It never reopens a
  closed row. When the room cannot be written, the closure is recorded locally
  (`<outbox>/closed/<ask_id>.json`) and applied by the next `reconcile` — but only for a
  question the outbox holds, or in outage mode (a row was confirmed here before); an unknown
  id with neither is refused and changes no count. The replay retires a local close only
  once its row is closed (or seen closed) AND complete — its body landed; a row the store
  view does not show is ambiguous and an incomplete one is still being filed, so in both
  cases the record stays and `reconcile` reports it.
- `remind` is the only way a reminder is sent (`src/check-pending-questions.py --notify`
  with this skill's adapter). Nothing is scheduled, and no pass surfaces questions: the
  owner is asked once, as questions come up, and reminded when he asks.

## The outbox

`<workspace>/state/pending-questions-outbox/<ask_id>.json` holds a question while the room
is unreachable: one file per ask, written atomically before the room write, never edited,
deleted only when the complete row carrying that exact ask id is confirmed. Its record,
the ask-id grammar and the close record are this skill's (`scripts/pending_questions_outbox.py`
over core's generic record directory, `src/local_record.py`: an ask id outside
`[A-Za-z0-9][A-Za-z0-9._-]{0,119}` is refused before any path is built, a file whose stem
does not match its inner id is skipped, and deletion stays inside the directory). `reconcile`
replays each entry through the normal add_row path (which resumes a row left incomplete), so
a replay is idempotent by ask id. Without a room capability or an owner room, every ask stays
in the outbox and `list` says why. Two markers under `state/` are kept apart:
`pending-questions-store-history` (a row of this workspace was confirmed; from then on no
store is an outage) and `pending-questions-db-introduced` (the owner was told once where the
database lives).

## Who reads it

This skill's adapter, `scripts/pending_questions_room_db.py`, is the single reader and
writer (`room_store`, `gather`, `waiting`, `count`, `reconcile_pass`, `resolve`, `ask_owner`,
`remind`); the manifest's `pending_questions_store` field declares it (`skills/MANIFEST.md`),
and the store, the outbox and its replay, the queue/routing of the owner message, the legacy
ingest and the reminder are this skill's (`scripts/pending_questions_store.py`,
`scripts/pending_questions_outbox.py`, `scripts/pending_questions_ask.py`,
`scripts/pending_questions_compat.py`, `scripts/pending_questions_remind.py`,
`scripts/pending_questions_ledger.py`). Core — the dashboard, the morning briefing,
agent-api, friction-detector, session-handoff, obsidian-mirror, the reminder entry — reaches
it only through `src/pending_questions_reader.py`, injecting the adapter `src/skill_roots.py`
finds by that field across `<repo>/skills` and `<workspace>/skills`, and reads no file. Every
one of them shows "unknown" while the room cannot be read — and with no adapter installed,
since core has no store of its own; the briefing says only the count and where to open it.
Without this skill, `scripts/ask-owner.py` only queues the owner's DM and keeps one generic
record under `<workspace>/state/ask-owner/`, and says that nothing lists or closes it.

## The room capability

The adapter looks for the room capability's scripts (`room_collab_client.py`,
`room_collab.py`) in the `room-commons` skill first, then its `room-collab` alias, under
`<workspace>/skills/` then `<repo>/skills/`. Nothing else in the repo depends on either.

## Migration from the per-host file

- **Existing `<workspace>/hosts/<host>/pending-questions.md` files** are read-only history.
  The one transitional reader, `scripts/pending_questions_compat.ingest_legacy_file_entries`
  (called from `reconcile`), copies each open entry into this host's row once — an entry
  with an `**Ask id:**` line and a settled `**Sent:**` record (#5003 and this branch's
  earlier heads), a `## ` section without one (main's prose) keyed by a digest of its text,
  or main's `- **[label, ts]** …` bullet — and marks it `**Status:** moved — as row q-…` in
  place ONLY after the row is confirmed complete.
- **Rolling upgrade.** While an old head still writes the file, the new head's next
  `reconcile` ingests those entries; nothing is lost in the window. The ingest is removed
  under `docs/migration-transition-window.md`: ~30 days of zero source-side writes, checked
  with `python3 skills/pending-questions/scripts/pending_questions_compat.py report` (the
  last mutation of each host's legacy file; the clock starts at the newest).
- **Persisted schedules.** The `pending-questions` entry is gone from
  `skills/schedule-crons/crons.example.json`. An old `crons.json` entry that still runs
  `python3 src/check-pending-questions.py` (with no flag or the retired `--reconcile-only`)
  now reconciles and lists; it sends nothing.

## Testing against an unreachable room

`PENDING_QUESTIONS_COLLAB_URL` (declared in this skill's `manifest.json` `config` block,
empty by default; the env override wins) points the adapter's collab service at that URL
for a run, so an outage can be rehearsed from the CLI:

```bash
PENDING_QUESTIONS_COLLAB_URL=https://unreachable.test.invalid \
    python3 skills/pending-questions/scripts/pq.py ask "outage rehearsal?" --urgency durable
PENDING_QUESTIONS_COLLAB_URL=https://unreachable.test.invalid \
    python3 skills/pending-questions/scripts/pq.py list
python3 skills/pending-questions/scripts/pq.py reconcile
```

`ask` leaves the question in the outbox (`recorded: OUTBOX …`), `list` says
`pending questions: UNKNOWN — room unreachable (…)` with the held question, and the
`reconcile` with the variable unset files it.
