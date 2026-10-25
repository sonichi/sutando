---
name: status-ack
description: Live-agent runner that tells a requester, once, that their AG2 Space task was received when no session has picked it up and no activity card or progress notice shows it by its first-ack deadline, and tells the owner once per outage when only the standby watcher is reading the inbox. Off by default (STATUS_ACK_ENABLED). Not slash-invoked.
---

# status-ack

The core cannot say "received" while it is deep in a long turn or while its session watcher is
gone. This runner works outside the core's turn. On each pass it decides, per queued task,
whether the requester is owed one status line. The decision comes from `src/status_ack_policy.py`
and is made from observed facts only.

## Scope

It speaks only for a task **no session has picked up** (no RUNNING phase, no worker holding it)
that shows **no activity card** (no row projected or owed in `state/activity/<task>.json`) and
**no sent progress notice** (task-progress records `progress:<task>:sent` after a send; its first-notify marker is bookkeeping only). The predicate is
`src/ack_overlap.py`. The gateway asks it again just before it sends the ack, for that ack's own
outbox item only, so a pickup after publication withdraws the line and nothing else. Anything else is
the card's and the progress notifier's to report, so this runner stays silent. A task a session
claimed and then went quiet on is out of scope here. The second thing it does is the owner DM
when the watcher is down.

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
| `STATUS_ACK_PYTHON` | empty | The interpreter sparrowd runs the hosted loop with. Empty: sparrowd skips the worker. |

## Running

The manifest declares a `supervised_worker` (`scripts/status_ack_worker.py`), so `sparrowd`
hosts the loop beside the gateway bridge (`skills/MANIFEST.md`). Nothing runs until both are set:
`STATUS_ACK_PYTHON` names an interpreter, which sparrowd reads when it starts, and
`STATUS_ACK_ENABLED` is true. While off, the loop reads and writes nothing and keeps waiting.

```
python3 skills/status-ack/scripts/status_ack.py --workspace <workspace> [--loop]
```

The runner is meant for the always-up live agent. It is not a core-session step, and guest
or one-shot agents never run it. Each pass prints one JSON report: what it spoke, what it
withdrew, what it skipped and why, and which claims it settled.

## Restart safety

- **One runner per workspace.** The loop holds `state/status-ack/runner.lock`; a second runner
  runs no pass while the first holds it.
- **Startup recovery.** On its first pass, a claim this runner holds from a process that is gone,
  whose file was never published (not in `results/`, not claimed by the gateway, not in
  `results/archive/`, no outbox receipt), becomes `unknown`: it is never released and never
  re-announced. A claim whose file was published is left for the receipt to settle; a claim a
  live process holds is left alone. Confirmed and unknown claims are never touched.
- **First-seen.** A task without an intake stamp is anchored at its first sighting, recorded
  create-only in `state/status-ack/first-seen/`, so a restart neither restarts its deadline nor
  loses it. Tasks that arrived while the runner was down get at most one line each.
- **Bounded.** Hourly, first-seen records and ack claims of tasks that left the queue, and
  alerts of outages that ended, are pruned after seven days; first-seen is also capped. A queued
  task keeps every record.

## State

- `<workspace>/state/ack-claims/`: one record per ack key, shared with `task-progress`.
- `<workspace>/state/status-ack/watcher-outage.json`: when the current standby-only outage began.
- `<workspace>/state/status-ack/first-seen/`: one create-only record per task, pruned as above.
- `<workspace>/state/status-ack/runner.lock`: held by the one live runner.
