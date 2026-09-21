---
name: startup
description: "Single entry point for fresh-session bootstrap. Runs optional task-orphan recovery, cron registration, and watcher start in a fixed order. Replaces the current `claude -- '/schedule-crons'` invocation pattern as the canonical CLI startup target."
user-invocable: true
---

# Startup

The canonical entry point for a fresh Sutando session. Bundles every action that must happen once at session start, in the correct order.

**Usage**: `/startup`

ARGUMENTS: $ARGUMENTS — `--worker` selects the worker bootstrap below; empty is the canonical core.

## Worker mode (`/startup --worker`)

A pool worker is an instance, not the canonical core. It shares the host's workspace but owns exactly one thing: the watcher on its own delivery folder. **When `$ARGUMENTS` contains `--worker`, run ONLY the sequence in this section and none of the steps below it** — no orphan check (the core owns `tasks/`), no `/schedule-crons` invocation, no ceremony gate (it stamps the core's `schedule-crons-stamp.json`), and no `/startup complete` line. The one exception is step 2.5 below: a `crons.json` entry explicitly pinned to THIS worker's own id (via its `owner` field, see `skills/schedule-crons/SKILL.md`) is this worker's to register — that is not "a second registrant" for it, because `/schedule-crons` skips any entry owned by a worker (`src/cron_ownership.py`). Every entry without an `owner`, or owned by a different worker, stays untouched here.

1. Ask whether this instance's own watcher is already live. **The core's step 1.5 gate is the wrong question here** — it is satisfied by *any* watcher tree on the host, and the core's own always satisfies it, so a worker that consults it never starts the watcher it exists to run:

   ```bash
   "${SUTANDO_PY:?the launcher forwards the resolved interpreter}" "$SUTANDO_WORKER_BOOTSTRAP"
   ```

   The gate belongs to the optional `worker-pool` skill, not to this one, so it is **named by the spawner in the session's env** and never spelled as a path here — a core install without that skill must still boot. If `$SUTANDO_WORKER_BOOTSTRAP` is unset or names no file, treat it as `unknown`: say so and start nothing.

   It answers about THIS instance's sentinel (`util_paths.watcher_sentinel_path`, the same file the watcher stamps) in the assigned workspace — read from the `SUTANDO_WORKSPACE_DIR` variable the spawner sets and the watcher honours, so gate and watcher can never inspect two trees — and prints one word: `start`, `skip`, or `unknown`.

2. On `start` only, start the streaming watcher via the `Monitor` tool — `command: 'bash "$SUTANDO_WATCHER_CMD" "$SUTANDO_TASKS_DIR"'`, `persistent: true`. Both are absolute and come from the launcher, because this session's cwd is the spawner's `--cwd` and need not be the repo — a relative `src/watch-tasks-stream.sh` exits 127 there. If `$SUTANDO_WATCHER_CMD` is unset you were not launched as a worker: stop and say so rather than guessing a path.

2.5. **On `start` only** (same gate as step 2 — a fresh incarnation; a `skip` means this instance is already running and re-registering here would duplicate whatever it already has), register any `crons.json` entries pinned to THIS worker's own id. There is no in-session memory of a prior registration to compare against the way `/schedule-crons` step 3 has for a mid-session re-run, so this only ever needs to handle the fresh-boot case:

   ```bash
   REPO="$(cd "$(dirname "$SUTANDO_WATCHER_CMD")/.." && pwd)"
   H="$(bash "$REPO/scripts/sutando-config.sh" host-label)"
   CF="$SUTANDO_WORKSPACE_DIR/hosts/$H/crons.json"
   "$SUTANDO_PY" -c "
   import json, sys
   sys.path.insert(0, '$REPO/src')
   from cron_ownership import entries_for_owner
   try:
       cfg = json.load(open('$CF'))
   except FileNotFoundError:
       cfg = []
   print(json.dumps(entries_for_owner(cfg, '$SUTANDO_INSTANCE_ID')))"
   ```

   The repo root is derived from `$SUTANDO_WATCHER_CMD` rather than assumed from cwd, for the same reason step 2 does. For each entry the filter returns, `CronCreate` it exactly as `/schedule-crons` step 3 would (`prompt_skill` → `/skill-name`; otherwise the `prompt` string) — an entry pinned to a worker is never a `monitor`, `execution: "codex-task"`, or `launchd: true` entry (those ownership models are core-only), so no further filtering is needed. If `crons.json` doesn't exist, or nothing is pinned to `$SUTANDO_INSTANCE_ID`, there is nothing to do here — that is the common case for most workers.

3. Report what actually happened, never a fixed line: `start` → `worker <instance> ready: watching <inbox>` (plus `+ N cron(s) registered` when step 2.5 registered any); `skip` → `worker <instance> already watching <inbox>` (nothing was started); `unknown` → start NOTHING and report the gate's `why=` verbatim. Reporting readiness after an `unknown` is the failure this step exists to prevent: it claims a watcher that does not exist.


## What this replaces

Previously: `claude -- "/schedule-crons"` was the de-facto startup invocation, and `skills/schedule-crons/SKILL.md` accumulated startup ceremony (cron-fallback, watcher) on top of its actual job (registering crons from `crons.json`).

Now: `claude -- "/startup"` is the canonical startup target. `/startup` orchestrates the sequence; `/schedule-crons` shrinks back to its narrow job.

Migration: update `~/Library/LaunchAgents/*.plist` and any CLI invocation scripts to call `/startup` instead of `/schedule-crons`. `/schedule-crons` still works standalone (for manual cron re-registration) — both paths are idempotent.

## Why one bundled skill

Per Chi 2026-05-23 Discord: "we can make a new skill and include everything we need at start." Five rationales:

1. **Single entry point** — no more "which skill does the CLI invoke?" The launchd plist points at `/startup` and only at `/startup`.
2. **Ordering encoded in one place** — the sequence (recover state → register schedules → start watcher) lives in this skill's `On Activation` section, not scattered across schedule-crons's step list.
3. **Easy to extend** — future startup work (new lifecycle checks, telemetry pings, dependency probes) appends to this skill's sequence; no debate about where it belongs.
4. **Each sub-step stays callable standalone** — `/task-orphan-check`, `/schedule-crons`, etc. continue to work for manual invocation. `/startup` is a wrapper, not a replacement.
5. **Idempotent re-invocation** — calling `/startup` twice in the same session is safe; each sub-skill is idempotent (registering an already-scheduled cron is a no-op, an already-running watcher isn't restarted, etc.).

## On Activation

The sequence below MUST run in this order. Each step is naturally idempotent, so re-invocation is safe.

### Step 1 — Task orphan check (optional)

Invoke `/task-orphan-check` IF the skill is installed (i.e. `$CLAUDE_CONFIG_DIR/skills/task-orphan-check/` exists). This is the recovery half of the post-#1049 redesign: scan `<workspace>/tasks/` for orphan tasks left over from a crash mid-execution, cross-reference per-side-effect markers (e.g. PR #1048's `.sending` files), archive completed tasks, write recovery sentinels for stuck ones. See the skill itself for the full procedure.

If the skill is not installed, skip silently. `/startup` works without it — every other step is independent.

Note: this step runs BEFORE step 2 so that the watcher (started by step 2's downstream) doesn't pick up an orphan task before recovery has classified it.

### Step 2 — Register schedules + start watcher

Invoke `/schedule-crons`. This handles:
- Reading `<workspace>/hosts/<hostname>/crons.json` (`<hostname>` = `bash scripts/sutando-config.sh host-label`) — the canonical per-host config.
  **Not `skills/schedule-crons/crons.json`.** That path is git-ignored and installer-managed: `src/init.sh` seeds it from `crons.example.json` when it is absent and leaves it untouched when it is present. So the in-checkout copy holds whatever the host last left there — shipped sample jobs, or a stale legacy schedule — and nothing keeps it in step with the per-host file. `skills/schedule-crons/SKILL.md` records the move; `/startup` was never updated to match, so registering from the in-checkout copy silently replaces the host's real schedule with legacy state.
- Starting the streaming task watcher via the `Monitor` tool (`bash src/watch-tasks-stream.sh`, persistent, description `"Streaming task watcher"`) — **first**, before any cron is registered (2026-08-24: moved ahead of registration so a task arriving during the registration loop isn't queued unprocessed; see `skills/schedule-crons/SKILL.md` step 1.5 for the measured impact)
- Calling `CronCreate` for each entry that isn't already scheduled
- Invoke the skill rather than hand-rolling `CronCreate` from this list: `/schedule-crons` also writes
  `<workspace>/hosts/<hostname>/schedule-crons-stamp.json`, which health-check's `session-crons` probe reads —
  a hand-rolled registration leaves that probe reporting the crons as never registered.
- Ensuring a fallback `/proactive-loop` cron exists at `*/10 * * * *` if `crons.json` doesn't include one (post-#954 belt-and-suspenders)

### Step 2.5 — Re-arm connector waits

```bash
python3 skills/connect-apps/scripts/connectors.py rearm
```

Restarts the background waiter of every connect-apps wait that is still pending (an app the owner was asked to connect before this session started), so their request is still answered once they sign in. Idempotent: a wait whose waiter is alive is left alone. Skip silently if `skills/connect-apps/` is absent; a non-zero exit is reported, never retried.

### Step 3 — Verify, then confirm

**Run the ceremony gate BEFORE claiming completion.** It is health-check's `session-crons` probe
— the same stamp-vs-session-boundary test the desktop app's ceremony-health uses to decide whether
to re-send `/startup` — so the agent sees the app's criterion at the one moment it can act on it:

```bash
python3 skills/startup/scripts/verify-ceremony.py    # rc 0 = stamped this boot; rc 1 = NOT complete
```

- **rc 0** → emit the one-line summary so the operator (or main session's first turn) sees what fired:

  ```
  /startup complete: orphan-check (N tasks recovered, M archived), schedules (K crons + watcher).
  ```

  The orphan-check fields say `skipped (skill not installed)` if step 1 was skipped.
- **rc 1** → do NOT print `/startup complete`. Print the gate's output verbatim, invoke `/schedule-crons`
  (the only writer of `hosts/<hostname>/schedule-crons-stamp.json`), and re-run the gate. A hand-rolled
  `CronCreate` passes every cheap check — `CronList` looks perfect and the cron fires — and still fails
  this one, which is exactly why the app kept re-sending `/startup` to a session that believed it was
  done (135 sends across four episodes, the longest 25 h, stamp unchanged in every one).
- **rc 2** → the probe could not run; say so and do not claim completion.

## Sequence diagram

```
session start
    │
    ▼
/startup
    │
    ├─► step 1:  /task-orphan-check (optional) ──► classifies + archives orphan tasks
    │
    ├─► step 2:  /schedule-crons ──┬─► step 1.5 (start watch-tasks-stream.sh via Monitor — FIRST, before registration)
    │                               ├─► step 2-3 (register crons.json entries)
    │                               ├─► step 4 (proactive-loop fallback if missing)
    │                               └─► step 6 (confirm what was scheduled)
    │
    ├─► step 2.5: connect-apps rearm ──► restarts waiters of pending connector waits
    │
    └─► step 3: emit summary
```

## Re-invoking in an already-running session

If `/startup` is invoked mid-session, the sub-skills skip their already-done work (an already-scheduled cron isn't re-created, an already-running watcher isn't restarted), so the result is effectively a re-confirm of state. Safe.

## What lives elsewhere

This skill is intentionally a thin orchestrator. Logic lives in the sub-skills:

- **Orphan recovery**: `skills/task-orphan-check/` (separate PR, optional)
- **Cron registration + watcher start**: `skills/schedule-crons/`
- **Claude Code hook registration**: `src/agent/claude/cli/start-cli.sh` re-runs `src/install-claude-hooks.sh` before the core spawns — NOT `/startup`. An engine update replaces `<engine>/.claude/settings.json`; the launcher re-registers on every launch, so the hooks are in place deterministically at session start rather than depending on health-check `--fix` landing late on its timer (issue #3221). `health-check.py`'s `claude-hooks` probe is the witness: `all N owned hooks registered` on the first pass after a launch.

If you find yourself wanting to put logic IN `/startup`, ask whether it belongs in one of the sub-skills (or a new sub-skill) first. `/startup` is the order, not the work.

## Iteration log

- v0.1.0 — 2026-05-23 — initial draft. Per Chi 2026-05-23 Discord exchange about #1049 redesign ("make a new skill and include everything we need at start"). `/startup` becomes the canonical CLI entry; `/schedule-crons` remains callable for manual cron re-registration. Migration: launchd plists + CLI scripts switch to `/startup`.
- v0.2.0 — 2026-06-21 — removed the fresh-session briefing step and its session sentinel (that sub-skill was deleted). `/startup` now runs orphan-check → schedules + watcher → confirm; sub-skill idempotency replaces the former sentinel guard.
