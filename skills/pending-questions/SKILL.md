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
python3 skills/pending-questions/scripts/pq.py clicks [--json]
```

**Trigger: any time you would write a question for the owner into terminal or summary text, file
it instead** with `ask` (or `scripts/ask-owner.py`; add `--unless-open` to file nothing when an
open row already has that question — it reports `already open: [<id>]`). The terminal and a compaction summary reach no one.
This skill's `hooks/compact-carry.py` keeps owner questions out of compaction summaries and, after
one, has the core file any never-asked question it carried; `session-state.md` shows only the
count (`python3 src/pending_questions_reader.py summary`).

- `ask` queues the question to the owner (the task's conversation only for an
  owner-tier task in his DM; otherwise his DM), saves it to the outbox, writes the row born
  with its `**Sent:**` record, and once that exact row is confirmed complete commits the
  store-history marker and THEN deletes the outbox file — a marker that cannot be written
  keeps the file (`history: FAILED` in the report) so the local evidence outlives the
  failure. Only after that confirmation does it reconcile the older backlog. Room subprocess
  calls share one operation budget (90 seconds by default), so failures cannot renew the
  timeout for each older question; the held entries remain for a later explicit reconcile.
  The shared reconcile pass applies that budget to every caller, including `pq reconcile`
  and reminders. Resolving an answer shares it across reconciliation and the row close.
  If the outbox cannot hold the question, NO row is written: the owner is still
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
  Legacy ingest refuses an archive boundary hidden by damaged Markdown before creating
  rows or marking source entries moved; it reports the affected line numbers.
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

## Decision cards

A question asked with `--default-action` and/or `--option` whose destination is AG2 Space (its
task's bridge, else the bridge the owner was last active on) is asked as a decision card, not
text: `queue_question` creates a `choice` HITL request (`src/hitl/cards.py`, the one builder every
card producer uses) with runtime `pending-questions`, guard and `subject.ask_id` set to the ask id,
`turn_on_action`, and one button per choice: with a default, one per `ordered_options()` entry
(Approve first, carrying the default); with options alone, exactly the options as given, with no
Approve. The ask is ordered so that no card exists without a durable question: the card is built
and its id allocated, the question is held in the outbox and its row is written, both naming that
id (`**Sent:** card hitl_… for ag2space owner-dm (decision card) at …`), and only then is the card
stored for the AG2 Space gateway to post in the owner's DM. A process that dies before the card is
stored leaves the held question and no card; one that dies after leaves both. The ask reports
`stored, not yet shown: decision card hitl_…` — storing is not showing. Once a card exists, no
text is sent for that question. Text is the ask only when the card cannot be built, the question
cannot be held (then no card is stored at all), or the card is provably not stored; and for every
other bridge (only that gateway projects cards). `create_card` returns stored, not stored or
unknown: a store that raised but whose card file is there (a save that failed after its rename) is
stored, and one that cannot be checked is unknown; both are kept as the card, with no text. On a
definite failure the text goes to the question's own destination with its link, and then the
outbox entry and the row are rewritten to name that file (the row through the store's one body
write), so the next reminder sees a drained text, not an absent card. A death between the text
and the rewrite leaves a record naming an absent card, which the reminder reads as due: the
owner may be texted twice, never not at all.

The limit: nothing here can see the DM. If the gateway never posts the card (it is down, or skips
it), the owner is not asked by this skill until he reads the card: the reminder never sends text
while the question's card is live (pending or clicked), posted or not, nor when the store cannot
say. The card's own follow-ups are the HITL renotify's. When the gateway posted it, the question's
last-asked time is the gateway's `notified_at`. Only a card that is gone (never stored, or settled
without closing the row) leaves the question due, and the reminder then texts it, when it runs;
nothing is scheduled.

The click is owner-checked and recorded by the bridge (`chosen_action`), and with `turn_on_action`
the same relay task reaches the core headed `hitl_click: true`, its body the button label. That
header is how the core recognises it: it runs `python3 src/pending_questions_reader.py clicks`
(`pq.py clicks` is the same), which reaches this adapter's `clicks`. For every `pending-questions`
card in progress with a chosen action it closes the row as Answered in one guarded write that also
requires the owner's Status to be open (a room outage records the close locally like any other
close), and prints `[<ask-id>] chose <action-id> '<label>' -> <consequence>` from the card's own
record. The core acts only on `chose` lines, then runs `… ack <ask-id>` and answers `[no-send]`;
until the ack the card stays claimed, so a run that dies before the core acted replays the same
choice (`(replayed: not acknowledged yet)`) rather than losing it. The claim (`closing`) is stamped
on the card before the row is closed, so a run that dies between the close and its record finds
the row closed by code, not by the owner, and replays the choice instead of calling it stale.
`clicks` and `ack` run under one exclusive lock beside the HITL store
(`state/hitl/pending-questions-clicks.lock`), and the claim is a `pid:nonce` token: a claim another
live process holds is skipped, never cancelled; a dead holder's claim is taken over.

A click on a row already closed, by code or by the owner's own Status, is reported as "already
answered — not a decision" (`stale`) and its card is cancelled. A close that could not be recorded
at all still prints the choice, every run, until the core acks it; after the ack, later runs only
retry the close and print "acknowledged before, not a new decision". Nothing marks a choice as
handed over before the core has it: only the ack does. `resolve` retires (cancels) the question's card whenever it closes the row or the room
refuses the close. A Status the owner sets on the row himself does not retire the card (reconcile
does not touch cards); a later click on it is reported stale. 

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
