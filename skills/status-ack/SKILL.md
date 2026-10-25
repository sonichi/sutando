---
name: status-ack
description: Live-agent runner that tells a requester, once, that their AG2 Space task was received when the core has not answered it by its first-ack deadline, and tells the owner once per outage when only the standby watcher is reading the inbox. Off by default (STATUS_ACK_ENABLED). Not slash-invoked.
---

# status-ack

The core cannot say "received" while it is deep in a long turn or while its session watcher is
gone. This runner works outside the core's turn. On each pass it decides, per queued task,
whether the requester is owed one status line. The decision comes from `src/status_ack_policy.py`
and is made from observed facts only.

## What it says, and where

- **One initial line per task, at most.** It is sent only after the task's first-ack deadline,
  which is measured from intake and set per priority. The text is one of the policy's fixed
  templates. There is no estimate and no invented progress.
- **Only in the task's own conversation.** The room and thread come from the bridge-attested
  task envelope (`envelope_hmac` must verify). If the envelope does not verify, the source is not
  Matrix, or the room or thread cannot be verified, nothing is sent. There is no fallback room
  and no DM. The line goes out as `results/proactive-about-<task>--<ts>.to-ag2space.txt`. The
  gateway's proactive outbox lane threads it on the task's message and checks the task again just
  before the send. This skill does not post, retry or claim delivery itself.
- **The core's own "on it" and this line never both go out.** Both take `ack:<task>:initial` in
  `src/ack_claim.py`, and whichever claims it first is the only one that speaks. The core's
  first `notify.py --task-file` call for a task is its initial ack. Its later calls are progress
  and are never suppressed, and neither is the task's real answer.
- **Checked again before publish.** Between deciding and publishing, the runner re-reads the
  result, the activity phase and any pending cancel. If any of them has changed, it releases its
  claim and publishes nothing.
- **Counted as sent only with a receipt.** An ack is `confirmed` only once the outbox item is
  DELIVERED with an event id. A send withdrawn at dispatch releases the claim. Any other park
  leaves the claim `unknown`, and that ack is never retried.
- **Watcher down.** When only a standby watcher has held the inbox for
  `STATUS_ACK_WATCHER_DOWN_AFTER_S` seconds, the owner gets one DM through the proactive path
  saying so and naming the remedy (restart the core). This happens once per outage. The outage
  ends when a session watcher holds the inbox again. Room lines say "the owner has been told"
  only after that DM has a delivery receipt.

## Config (`manifest.json`, CLI > env > manifest)

| Key | Default | Meaning |
|---|---|---|
| `STATUS_ACK_ENABLED` | `0` | Off: a pass reads nothing and writes nothing. Turning it on is the owner's decision. |
| `STATUS_ACK_FIRST_ACK_DEADLINE_URGENT_S` / `_NORMAL_S` / `_LOW_S` | 30 / 120 / 600 | Seconds from intake to the first ack, by task priority |
| `STATUS_ACK_POLL_S` | 15 | Seconds between passes with `--loop` |
| `STATUS_ACK_WATCHER_DOWN_AFTER_S` | 120 | How long only a standby must hold the inbox before the owner alert |

## Running

```
python3 skills/status-ack/scripts/status_ack.py --workspace <workspace> [--loop]
```

The runner is meant for the always-up live agent. It is not a core-session step, and guest
or one-shot agents never run it. Each pass prints one JSON report: what it spoke, what it
withdrew, what it skipped and why, and which claims it settled.

## State

- `<workspace>/state/ack-claims/`: one record per ack key, shared with `task-progress`.
- `<workspace>/state/status-ack/watcher-outage.json`: when the current standby-only outage began.
