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
python3 skills/pending-questions/scripts/pq.py ack <ask-id> --token <token>
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
  owner is asked once, as questions come up, and reminded when he asks. The one exception
  completes that single ask: every pass, flagless included, sends the pointer for a decision
  card the gateway never posted (see The reminder, below).

## Decision cards (the ask)

A question asked with `--default-action` and/or `--option` whose destination is AG2 Space (its
task's bridge, else the bridge the owner was last active on) is asked as a decision card, built by
`src/hitl/cards.py` (the one builder every card producer uses): one button per choice — with a
default, one per `ordered_options()` entry, Approve first and carrying it; with options alone,
exactly the options as given, with no Approve — and `subject.choices` holding each consequence.

- **Order.** The card is built and its id allocated; the question is held in the outbox and its
  row is written, both naming that id (`**Sent:** card hitl_<id> for ag2space owner-dm (decision
  card) at <time>`); only then is the card stored, for the AG2 Space gateway to post in the
  owner's DM. No card exists without a durable question. The ask reports
  `stored, not yet shown: decision card hitl_<id>`: storing is not showing.
- **One surface.** Once a card exists, no text is sent for that question. `create_card` returns
  stored, not stored or unknown: a store that raised but whose card file is there, or one that
  cannot be checked, is kept as the card, and the report says no text was sent.
- **Text instead.** Only when the card cannot be built, the question cannot be held (then no card
  is stored), or the card is provably not stored; and for every other bridge. On a definite card
  failure the text goes to the question's own destination with the row's own link (the one the
  row write returned), and the question is held again, flagged `redeliver`, naming that file. For
  a landed row, in order: the store history is committed, the row is rewritten (one body write,
  `RoomDbStore.redeliver`), and only then does the held entry go — a history that cannot be
  written keeps the hold, so an outage never counts the question as zero;
  a row that could not be rewritten, or whose write was never confirmed, takes the record at the
  next reconcile, which replays the entry and rewrites the row before releasing it. A death
  before the hold leaves a record naming an absent card, which the next `--notify` reminder
  reads as due and texts.
- **The reminder.** It never sends text for a question whose card is live (pending or clicked) or
  whose store cannot say — `--force` included; such questions are left to the card. Once posted,
  the question's last-asked time is the gateway's `notified_at`. A live card with no post
  recorded 30 minutes (`CARD_POINTER_GRACE_SEC`) after it was stored gets a text pointer. Under the
  HITL store lock the card is re-checked (live, unposted, no pointer sent, no live claim) and a
  claim (a token with its own lease, `pointer_lease`) is stamped; only the claimant publishes,
  then marks it sent (`pointer_sent_at`). The claim's lease is the same elapsed-time lease as a
  click's (see Decision-card clicks): each reminder pass credits at most `POINTER_MAX_STEP_SEC`
  (150 s) and nothing for a backward step, and a claim whose lease has run 5 minutes
  (`POINTER_CLAIM_RETRY_SEC`) with no sent mark — its claimant died or failed to publish — is
  claimed again, with a new lease, and retried. So the pointer is at-least-once: overlapping
  reminders queue one per claim, a death before the publish is retried later, a death between
  the publish and the sent mark can deliver it twice, and no single clock step replaces a live
  claim (a rollback or a corrected spike moves its lease by at most one 150 s step).
  Just before the file is written the card is re-checked under the store lock
  (live, unposted, still this claim's), so a post recorded after the claim cancels the pointer;
  the gateway records a post only after its send returns, so the one window left is a send in
  flight — bounded by one projector send, and the pointer then only says the card may not have
  arrived. Marking a pointer sent never touches a settled or archived card. The pointer says the question waits as a card in
  his AG2 Space DM and may not have arrived, and quotes its title, with no options, no approve
  wording, and a note that a reply does not answer it. Only a card that is gone leaves the
  question due for text. The pointer step runs on every pass of `src/check-pending-questions.py`,
  flagless included (the proactive loop's pass, or an old schedule), outside presenter mode, so an
  unposted card is pointed to without anyone asking; the text reminders still need `--notify`.

## Decision-card clicks

A question's decision card is a HITL `choice` requirement with runtime `pending-questions`, guard
and `subject.ask_id` set to the ask id and `subject.choices` mapping each action id to what it
does. The bridge owner-checks a click, records it (`chosen_action`) and, the card asking for a
turn, forwards it to the core as a task headed `hitl_click: true`. The core then runs
`python3 src/pending_questions_reader.py clicks` (`pq.py clicks` is the same), which reaches this
adapter's `clicks` (`scripts/pending_questions_clicks.py`):

- Each pass runs under one exclusive lock beside the HITL store
  (`state/hitl/pending-questions-clicks.lock`); its holder is the only live claimant, so no pid
  is probed. A claim still marked `claiming` was left by a pass that died; it is retried with the
  same token.
- The pass claims the click (a token, saved atomically on the card) and closes the row as
  Answered in one guarded write that requires the owner's Status to be open. The same write puts
  a plain `Answered@<host>` in Closed (what a reader that predates click tokens understands) and
  the token in Recovery (`click:<token>@<host>`, which such a reader ignores). A refused close
  first checks the owner's Status: Answered or Resolved makes the click stale whatever any mark
  says. Otherwise a Recovery token equal to this claim's is the claim's own earlier close, and any
  other refusal (a close by code such as `resolve`) makes the click stale: "already answered — not
  a decision", and its card is cancelled.
- **Fail closed.** Only the room can say the owner's Status is open, so a click never closes
  through a local close record: when the room store is missing or unreachable, or the row is not
  in it yet, the click is deferred — nothing is handed over, the claim stays, and the next pass
  retries.
- On a close the claim is recorded `handed` (with `handed_at`) before it is returned. The choice
  prints once, as `[<ask-id>] chose <action-id> '<label>' -> <consequence> … ack with --token
  <token>`, to that caller alone. Later passes, by any consumer, print it as "handed out earlier,
  awaiting ack — not a new decision" for the lease (`HANDED_LEASE_SEC`, 10 minutes).
- The core acts, then runs `… ack <ask-id> --token <token>`, which settles the card. A handed claim
  whose lease has expired is re-offered as `chose` to the next pass, ONCE, with a fresh token and
  a new lease, so a consumer that died after the handoff does not strand the choice. After that
  single re-offer an unacked claim is listed as "NEEDS ATTENTION — handed out twice, never
  acknowledged" and never offered again; `clicks --due` (and so the health check) surfaces it.
- **The lease** (`scripts/pending_questions_lease.py`) counts elapsed time, not wall-clock
  deadlines. Invariant: it expires exactly when the time credited to it reaches its window
  (`HANDED_LEASE_SEC`, 600 s); each pass credits `clamp(now - observed_at, 0, max_step)` and then
  sets `observed_at = now`. With `LEASE_MAX_STEP_SEC` (300 s, the health check's fallback cadence)
  below the window: a backward step credits nothing, so a rollback never expires a lease or raises
  NEEDS ATTENTION; a forward spike of any size credits at most one step, and once the clock is
  corrected normal time resumes, so a spike never freezes recovery; repeated passes at one time
  credit 0; no single pass expires a fresh lease; a handoff or re-offer starts a new lease. The
  trade-off, chosen on purpose: catching back up after a rollback is credited again, so a rollback
  can over-credit by at most one step — bounded, where refusing all double credit would freeze the
  lease for the whole span of a corrected forward spike. The passes are every `clicks` and every
  `clicks --due` (the health check), each crediting and saving the lease atomically under the
  clicks lock, so a restart resumes from the saved credit.
- **A retired card wins.** Each claim save re-checks the card under the HITL store lock and is
  refused for a card already settled (a concurrent `resolve` retiring it); the pass then hands
  nothing over and returns no token, printing "settled meanwhile — not a decision". An
  ack carrying a superseded token is refused as stale and changes nothing.
- **At-least-once, at most twice.** The core acts before it acks, so a consumer slower than the
  lease, or a clock jump, can let a second consumer receive the same `chose`. Rule: before acting
  on a `chose`, re-check the current state (the PR is not already merged, the question is not
  already resolved, the thing is not already done) and skip what is.
- `resolve` retires (cancels) the question's card whenever it closes the row or the room refuses
  the close.
- **Wakeup.** A deferred click, or a handoff whose lease ran out, owes a pass with no new task to
  trigger it. `src/health-check.py` (run every proactive-loop pass, and by the launchd fallback)
  reads that through the core reader (`clicks --due`, whose only write is crediting each handed
  lease with this pass) and warns
  `decision-card-clicks`, naming `… clicks` to run; the core handles that output as for a
  `hitl_click` task. A host that never runs the health check retries only on the next click task.

A pass that dies after recording `handed` and before its caller printed the choice leaves it
handed but unseen; it is shown as awaiting ack until the lease runs out, then re-offered.

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
  now reconciles and lists; it sends nothing but an owed decision-card pointer.

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
