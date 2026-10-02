---
name: pending-questions
description: Ask the owner a decision you need, list what is waiting on him, close a question once he has answered, or run the reminder. Use when blocked on the owner's word, or asked "what are you waiting on me for?".
---

# pending-questions

One CLI over the owner's pending questions. The ledger is always the per-host
`<workspace>/hosts/<hostname>/pending-questions.md`; when a room and the room-collab
capability resolve, each question is also a row of the "Pending questions" database in
that room. Without them every verb works on the file alone.

The room is `PENDING_QUESTIONS_ROOM`: env, then this manifest's `config`, then the host's
`<workspace>/state/pending-questions-room`; unset, the owner's DM. Point every host of the
owner at one room shared by him and all his Sutandos; rows carry their Host.

```bash
python3 skills/pending-questions/scripts/pq.py ask "<question>" [--context "<why / options>"] \
    [--task-file <workspace>/tasks/<task>.txt] [--urgency live|durable] \
    [--default-action "<what Approve does>" --reason "<why>"] [--option "Hold=wait"] \
    [--priority High|Medium|Low]
python3 skills/pending-questions/scripts/pq.py list [--json]
python3 skills/pending-questions/scripts/pq.py resolve <ask-id> [--answered]
python3 skills/pending-questions/scripts/pq.py remind [--force]
```

- `ask` records the entry, queues it to the owner (the task's conversation only for an
  owner-tier task in his DM; otherwise his DM) and stamps `**Sent:**`. Read its output: a
  `FAILED` line is not an ask. Never hand-edit the ledger. Then continue; never block.
- `list` prints every question the reminder counts as waiting, each with its ask id.
- `resolve` closes an entry in the file and, when present, its database row, as
  Resolved (or Answered with `--answered`). It never reopens a closed row.
- `remind` runs `src/check-pending-questions.py` with this skill's adapter injected.

The reminder also finds the adapter without this CLI: the manifest's
`pending_questions_store` field declares it (`skills/MANIFEST.md`).
