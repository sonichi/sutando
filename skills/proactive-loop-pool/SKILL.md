---
name: proactive-loop-pool
description: "Pool-aware variant of /proactive-loop for the multi-core agent pool (#880). Each session in the pool runs this skill; the only behavioral diff vs /proactive-loop is a claim step before processing each task."
user-invocable: true
---

# Proactive Loop (Pool-Aware)

Variant of `/proactive-loop` that's safe to run in N parallel sessions sharing one workspace. Each core declares its own runtime — `claude` or `codex` — so a pool can mix both; this file is the Claude entry, [`CODEX.md`](CODEX.md) is the Codex one. The **only behavioral difference** from `/proactive-loop` is step 1 — task pickup goes through the atomic-rename claim before reading the task file. Losing the claim race means another session is processing the task; this session walks away. The rest of the loop body is unchanged.

This skill exists for the multi-core pool installed by `bash scripts/install-core-pool.sh N`. Each launchd-managed core session in the pool invokes `/proactive-loop-pool` instead of `/proactive-loop`.

**This file is the Claude entry.** The claim → finish protocol below is runtime-neutral; the entry is not. The Codex counterpart is [`CODEX.md`](CODEX.md) — one entry per runtime, sharing this protocol.

**Single-core users**: keep using `/proactive-loop`. This skill is only useful when N > 1 sessions exist; in single-core mode it adds claim overhead with no benefit.

**Usage**: `/proactive-loop-pool [interval]`

ARGUMENTS: $ARGUMENTS

## Required env vars

Each pool session sets these via its launchd plist (see `scripts/install-core-pool.sh`):
- `SUTANDO_CORE_ID` — this session's 1-based core ID (e.g. `1`, `2`, `3`).
- `SUTANDO_CORE_POOL_SIZE` — total pool size (informational; not enforced by this skill).
- Workspace resolved via `bash scripts/sutando-config.sh workspace` (env override retired post-#1440).

If `SUTANDO_CORE_ID` is unset, abort with a clear error: "proactive-loop-pool requires SUTANDO_CORE_ID — are you sure you meant to invoke this instead of /proactive-loop?"

## Activation (persistent follower form)

Followers are long-lived interactive sessions inside tmux (`tmux attach -t core-N` to observe), started by the launchd wrapper. On activation:

1. **Do NOT run `/schedule-crons`.** The host cron set (morning briefing, digests, main loop) is owned by the main core / lead; a follower registering it would fire every host cron N times. Followers register exactly ONE session cron: `CronCreate` with `cron: "*/5 * * * *"` and `prompt: "/proactive-loop-pool pass"` — the periodic sweep that catches assignments the watcher missed.
2. Start the streaming task watcher via the `Monitor` tool — `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`. React ONLY to events for your own assignments (`task-*.assigned-core-<your id>.txt`) plus, in leaderless fallback, unassigned `task-*.txt` (acquire_work decides — see below). Ignore other instances' assigned/claimed events.
3. Liveness is the wrapper's job (`pool-follower-beat.sh` writes `state/cores/core-N.alive` pid-bound to this session) — do not start `core_heartbeat.py` in-session.
4. Do not write `core-status.json` — that file is the main core's owner-facing status; a follower overwriting it lies to the owner about what the main core is doing.

When the arguments say `pass`, run one acquire-and-process sweep (steps below) and stop; otherwise this is session boot — do steps 1-2, run one sweep, then idle until watcher events or the cron fire.

## The claim step (what's different from /proactive-loop)

When the task watcher emits `TASK_FILE: <basename>` for a new task, **before** reading the task body, run the claim step. There are two flavors:

### Plain claim (no channel affinity)

For voice / phone / un-channeled tasks, or as a fallback when the task body has no `channel_id:` field:

1. Extract the task ID from the filename (`task-<id>.txt` → `<id>`).
2. Run: `python3 src/pool_follower (acquire_work).py <id> $SUTANDO_CORE_ID`
3. Exit 0 → claim won, read the printed path; Exit 1 → skip; Exit 2 → log and skip.

### Channel-affinity claim (Discord / Telegram / Slack)

For tasks with a `channel_id:` field in the body (`#884`):

1. Extract the task ID from the filename.
2. Peek at the task body (without claiming yet) to read the `channel_id:` line. (Use `head` / `grep` — don't run a full Read tool yet since other cores may grab the task first.)
3. Run: `python3 src/claim_task.py <id> $SUTANDO_CORE_ID <channel_id>`
4. Outcomes:
   - **Exit 0** → claim won. Script prints the renamed path (`tasks/task-<id>.claimed-core-<n>.txt`). Read THIS path. Your core is now the channel's handler for the next 30 min (default `SUTANDO_CORE_IDLE_THRESHOLD_SEC`).
   - **Exit 1** → respect-handler OR lost-race. Either another core is the channel's active handler, or another core won the race-claim. Skip this task entirely.
   - **Exit 2** → validation error. Log and skip.

The affinity machinery is **inside** `claim_task.py` — your only responsibility is to pass `channel_id` when present. The script reads `state/cores/channel-<id>.handler` and `state/cores/core-<n>.alive` to decide whether you're allowed to claim.

Use the renamed `task-<id>.claimed-core-<n>.txt` path for all subsequent reads + result writes.

**Core attribution (required, owner request 2026-08-23):** end every user-facing result body with a final line naming your worker, em-dash form: `— worker-<n>` (formerly core-<n>; owner renamed 2026-08-31 — one core, N workers). Plain text only — never a bracketed form (`[core-N]` would trip ag2space's `team_result_guard`, which withholds bodies carrying bracketed control markers). Skip the signature only on `[deduped:]`/`[no-send]` bodies, which no user reads.

**Completion step (required):** compose every result body starting with the line `task: <id>` (the id from your claimed file's name), then complete via the helper — one command replaces the manual write/flag/archive trio:

```bash
python3 src/pool_follower.py finish tasks/task-<id>.claimed-core-<n>.txt core-<n> <<'EOF'
task: <id>
<result body>
EOF
```

The `task: <id>` first line is a pairing check: the helper refuses (exit 2, zero writes) if it doesn't match the claimed file's id — this is what prevents a session holding two claims from writing each reply into the other task's result file. The helper strips that line before writing, so users never see it, then writes `results/task-<id>.txt` atomically, touches your done-flag, and archives the claimed file under its canonical name (`tasks/archive/task-<id>.txt` — result consumers resolve by that name; a claimed-suffix archive name dead-letters the reply) — in that order. It last appends one line to `data/pool-metrics.jsonl` (`task_id`, `core`, `source`, `arrived_at`, `finished_at`, `duration_s`) — the pool's only record of how long anything takes; that append is fail-open and can never turn a delivered answer into a failed task. Never hand-write the result/flag/archive steps yourself.

**Initial sweep on session start**: the watcher's initial sweep emits TASK_FILE events for any pre-existing files. Run the claim step on each; expect to win some and lose others depending on which sibling session got there first.

### Why channel affinity matters

Without it, three follow-up Discord messages on the same topic would scatter across the 3 cores. Each core would run a partial proactive-loop pass and write a partial result; the latest task's result would carry the consolidated reply via `[deduped:]` marker. That's wasteful — 3× quota burn for 1× user-visible reply.

With affinity, all 3 tasks land on the same core; that core has the conversational context in its session memory and produces one coherent reply. Quota burn matches what a single-core setup would have done.

## The rest of the loop

Identical to `/proactive-loop`'s numbered steps 2-11:

2. Check pending questions.
3. Check system health.
4. Read the build log.
5. Pick highest-ROI work.
6. Act on it.
7. Update build_log.
8. If blocked, ask.
9. Ensure the streaming watcher is running.
10. Monitor Discord.
11. Heartbeat.

These steps run independently per session. Quota / active-engagement / presenter-mode skip conditions all apply per-session — each pool member checks them on its own pass.

## Crash recovery (both former Phase 2b gaps are closed)

A crashed follower no longer needs an owner to rename anything by hand. The lead
sweeps every pass (`scripts/pool-lead-daemon.py` → `src/runtime-api/pool_lead.py`):

- `reclaim_dead()` / `reclaim_stuck_assignments()` — an assignment a dead or
  wedged core never claimed goes back to the pool.
- `reclaim_claimed()` — a claim held by a core that died is restored to its
  canonical name so bridges can deliver it.
- `prune_done_flags()` — retires flags whose task is long gone.

**The done-flag is a gate, not a marker.** `reclaim_claimed` treats a task as
delivered only when the done-flag **and** result evidence both exist. A done-flag
*alone* means the core died between flagging and writing the result — nothing
user-visible happened — so the task is repooled for reassignment rather than
silently dropped. That is what stops a crash-then-replay from firing a side
effect twice, and it is why `finish_task` writes the result before the flag.

Still true, and worth knowing: a follower gets no second chance from *inside*
its own session. Recovery is the lead's job, so a pool with a dead lead
degrades to leaderless claiming and stops reclaiming.

## Disabling the pool

To revert to single-core:
1. Remove `SUTANDO_CORE_POOL_SIZE` from `.env` (or set to 1).
2. Run `bash scripts/uninstall-core-pool.sh` to remove the launchd plists.
3. `bash src/restart.sh` to restart the foreground core.

## Lead-follower mode (L2+, supersedes raw claiming)

Each pass, acquire work via the assignment loop — NEVER claim unassigned
tasks directly while the lead is alive:

```python
import sys; sys.path.insert(0, "src")
from pool_follower import acquire_work
got = acquire_work(WORKSPACE/"tasks", WORKSPACE/"state",
                   f"core-{CORE_ID}", "pool-lead")
```

`acquire_work` returns your claimed task path or None. It honors lead
assignments in priority order and degrades to leaderless claiming only when
`pool-lead.alive` is stale/absent/future-dated. Done-flags before side
effects, exactly as below.
