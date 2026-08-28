---
name: proactive-loop-pool
description: "Pool-aware variant of /proactive-loop for the multi-core agent pool (#880). Each session in the pool runs this skill; the only behavioral diff vs /proactive-loop is a claim step before processing each task."
user-invocable: true
---

# Proactive Loop (Pool-Aware)

Variant of `/proactive-loop` that's safe to run in N parallel claude sessions sharing one workspace. The **only behavioral difference** from `/proactive-loop` is step 1 — task pickup goes through the atomic-rename claim before reading the task file. Losing the claim race means another session is processing the task; this session walks away. The rest of the loop body is unchanged.

This skill exists for the multi-core pool installed by `bash scripts/install-core-pool.sh N`. Each launchd-managed core session in the pool invokes `/proactive-loop-pool` instead of `/proactive-loop`.

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

When the task watcher emits `TASK_FILE: <basename>` for a new task, **before** reading the task body, run the acquisition step. There is exactly one:

```bash
WORKSPACE="$(bash scripts/sutando-config.sh workspace)"
python3 src/pool_follower.py acquire "$WORKSPACE/tasks" "core-$SUTANDO_CORE_ID"
```

Resolve `WORKSPACE`; do not expect to inherit it. `pool-core-wrapper.sh` passes
the child exactly `CLAUDE_CONFIG_DIR`, `SUTANDO_CORE_ID` and
`SUTANDO_CORE_POOL_SIZE`, so an unset `$WORKSPACE` expands to empty and the
command becomes `acquire "/tasks" ...`, which exits 2 and claims nothing. The
resolver is cwd-relative and the wrapper starts the session in `POOL_REPO_DIR`.

Exit codes are the contract:

- **0** — you hold the task. The claimed path is printed on stdout; read THAT path, not the name the watcher gave you (it has been renamed to `.claimed-core-<n>.txt`).
- **1** — nothing for you this tick. An ordinary idle result, not an error: either a live lead has not assigned you anything, or another follower won the race. Skip and carry on.
- **2** — usage or environment error. Log and skip.

Do not extract the task id and do not peek at the body first. `acquire` scans the directory itself, honours your own assignments in priority order, and only opens the unassigned pool when the lead's heartbeat has gone stale. Reading before claiming is what lets two sessions execute the same task.

**Channel affinity is the lead's job, not yours.** Under a live lead the assignment you receive has already had affinity applied, so there is no separate channel-affinity claim to make. `src/claim_task.py` is the pre-pool leaderless claimer; it does not recognise `.assigned-core-<n>` names and returns None for them, so calling it on an assignment silently does nothing.

Use the renamed `task-<id>.claimed-core-<n>.txt` path for all subsequent reads + result writes.

**Core attribution (required, owner request 2026-08-23):** end every user-facing result body with a final line naming your core, em-dash form: `— core-<n>`. Plain text only — never a bracketed form (`[core-N]` would trip ag2space's `team_result_guard`, which withholds bodies carrying bracketed control markers). Skip the signature only on `[deduped:]`/`[no-send]` bodies, which no user reads.

**Completion step (required):** compose every result body starting with the line `task: <id>` (the id from your claimed file's name), then complete via the helper — one command replaces the manual write/flag/archive trio:

```bash
python3 src/pool_follower.py finish tasks/task-<id>.claimed-core-<n>.txt core-<n> <<'EOF'
task: <id>
<result body>
EOF
```

The `task: <id>` first line is a pairing check: the helper refuses (exit 2, zero writes) if it doesn't match the claimed file's id — this is what prevents a session holding two claims from writing each reply into the other task's result file. The helper strips that line before writing, so users never see it, then writes `results/task-<id>.txt` atomically, touches your done-flag, and archives the claimed file under its canonical name (`tasks/archive/task-<id>.txt` — result consumers resolve by that name; a claimed-suffix archive name dead-letters the reply) — in that order. Never hand-write the result/flag/archive steps yourself.

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

## Phase 2a known limitations

This skill ships in Phase 2a of #880. Two pieces are NOT yet wired in:

- **Done-flag side-effect gate** (Phase 2b). Without it, the rare crash-then-replay window can fire a side effect twice. Mitigation today: rare crashes within the few-second window between claim and side-effect-completion.
- **Boot-time orphan watchdog** (Phase 2b). If a pool session crashes after claiming but before processing, the claim file is stranded until owner manually renames it back. Mitigation today: `launchctl bootout <core> && launchctl bootstrap <core>` re-runs the session which won't re-claim a stale file (but won't release it either — manual rename needed).

For "let me try it tonight" the limitations above are acceptable. Phase 2b ships the watchdog + done-flag gate.

## Disabling the pool

To revert to single-core:
1. Remove `SUTANDO_CORE_POOL_SIZE` from `.env` (or set to 1).
2. Run `bash scripts/uninstall-core-pool.sh` to remove the launchd plists.
3. `bash src/restart.sh` to restart the foreground core.

## Lead-follower mode (L2+) — what acquisition guarantees

This section explains the semantics. The command is the one in "The claim
step" above; do not invoke `acquire_work` a second way from here.

Never claim unassigned tasks directly while the lead is alive. `acquire`
enforces that for you: it honours your own lead assignments in priority order,
and opens the unassigned pool only when `pool-lead.alive` is stale, absent or
future-dated. Done-flags do NOT land before side effects — the only
production writer runs after the result is published, so the at-most-once
floor promised by that ordering does not exist yet (see Phase 2a known
limitations above, and `docs/lead-follower-pool.md` step 4).
