---
name: role-status
description: Verify and publish a delegated role-status judgment for a `task-role-status-v1-<sha>-a<N>` task — provenance-strip every cited id against the task file, refuse under-coverage, and write the `[no-send]` result atomically. Invoked by the core for every such task; never slash-invoked by the owner.
---

# role-status

The sutando-life app files `tasks/task-role-status-v1-<sha>-a<N>.txt`: bounded
evidence (one `EVIDENCE_JSON` line) plus a caller contract asking for rows of
`{actor_id, working_event_ids, blocked_items}`. The judgment is delegated to a
small model; this skill is the gate between that model's answer and the result
file. A model can fabricate an event id, cite another actor's event, or answer
for one actor of nine — the verifier strips the first two and refuses the third.

## Contract — the core follows this for every `tasks/task-role-status-v1-*.txt`

1. **Delegate the judgment** to an efficient model (the task's `model_hint`),
   passing the task file's caller contract and evidence verbatim. The model
   writes a **bare JSON array** to a scratch file, e.g.
   `<workspace>/state/role-status/<task-id>.judgment.json` — no prose, no
   markers. A leading `[no-send]` line is tolerated and dropped.
2. **Publish through the skill, never by hand:**

   ```bash
   python3 skills/role-status/scripts/publish.py <task-file> <judgment.json>
   ```

   `<task-file>` is the attempt file that **still exists** under `tasks/`
   (`-a1`, `-a2`, `-a3` …). The producer withdraws an attempt and reissues the
   next one; a result written against a withdrawn attempt is never claimed, so
   re-list `tasks/` right before publishing.
3. **The result** is `[no-send]` followed by the verified JSON array, written
   create-if-absent (temp file in `results/` + hard link, which fails when the
   name exists) to `<workspace>/results/<task-id>.txt`. Nothing else is
   written; the bridge archives the task without a user-visible reply.
4. **Exit codes:**
   - `0` — published.
   - `1` — refused on coverage: fewer distinct verified actors than
     `ceil(0.5 × actors with events)`. **Nothing written.** Re-run the delegate
     with an explicit coverage instruction ("answer for every actor that has an
     event; N actors are listed") and publish again.
   - `2` — cannot answer: unreadable task or judgment, malformed or duplicated
     `EVIDENCE_JSON`, event ids present but none resolvable to an owner
     (`cannot answer: no event ownership resolvable`), a result already
     published (`cannot answer: result already published`). **Nothing
     written**, any existing result untouched. Do not retry the model;
     surface the task to the owner.

`publish.py` delegates every judgment decision to `scripts/verify.py`
(`verify.verify(task_text, judgment, min_coverage)`); the verifier remains
usable on its own for inspection:

```bash
python3 skills/role-status/scripts/verify.py <task-file> <judgment.json> [--out <verified.json>] [--min-coverage 0.5]
```

## What the verifier enforces

- **Provenance.** A row's `actor_id` must occur verbatim in the task file, and
  every cited event id (`working_event_ids`, `blocked_items[].evidence_event_ids`)
  must be **owned** by that actor. Ownership is read only from the object
  following the single `EVIDENCE_JSON` marker line; an actor mentioned inside
  another actor's `detail` owns nothing. A file with no marker falls back to
  blank-line blocks, where an id is owned only when its block names exactly one
  actor — a multi-actor block is ambiguous, its ids are owned by nobody, and its
  actors still count toward the floor. On either path an event id claimed by
  more than one actor is ambiguous the same way. Ids are whole tokens: an id
  touching another id character on either side (`…:ag2.spacex`, `$abcd` inside
  `$abcde`) is a different token and never matches.
- **Coverage.** `distinct verified actors ≥ ceil(min_coverage × actors_with_events)`,
  else refused. Duplicate rows for one actor count once. A file with event ids
  none of which resolves to an owner is exit 2, never an empty `[]` publish;
  only a file with no event ids at all (nobody working) answers `[]`.
- **Fail closed.** A malformed marked object or two marker lines is exit 2,
  never the block fallback and never a zero floor.

Tests: `tests/skills/role-status/`.
