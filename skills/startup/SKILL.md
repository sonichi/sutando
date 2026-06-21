---
name: startup
description: "Single entry point for fresh-session bootstrap. Runs optional task-orphan recovery, cron registration, and watcher start in a fixed order. Replaces the current `claude -- '/schedule-crons'` invocation pattern as the canonical CLI startup target."
user-invocable: true
---

# Startup

The canonical entry point for a fresh Sutando session. Bundles every action that must happen once at session start, in the correct order.

**Usage**: `/startup`

ARGUMENTS: $ARGUMENTS (currently unused — reserved for future per-instance overrides)

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

Invoke `/task-orphan-check` IF the skill is installed (i.e. `~/.claude/skills/task-orphan-check/` exists). This is the recovery half of the post-#1049 redesign: scan `<workspace>/tasks/` for orphan tasks left over from a crash mid-execution, cross-reference per-side-effect markers (e.g. PR #1048's `.sending` files), archive completed tasks, write recovery sentinels for stuck ones. See the skill itself for the full procedure.

If the skill is not installed, skip silently. `/startup` works without it — every other step is independent.

Note: this step runs BEFORE step 2 so that the watcher (started by step 2's downstream) doesn't pick up an orphan task before recovery has classified it.

### Step 2 — Register schedules + start watcher

Invoke `/schedule-crons`. This handles:
- Reading `skills/schedule-crons/crons.json`
- Calling `CronCreate` for each entry that isn't already scheduled
- Ensuring a fallback `/proactive-loop` cron exists at `*/10 * * * *` if `crons.json` doesn't include one (post-#954 belt-and-suspenders)
- Starting the streaming task watcher via the `Monitor` tool (`bash src/watch-tasks-stream.sh`, persistent, description `"Streaming task watcher"`)

### Step 3 — Confirm

Emit a one-line summary so the operator (or main session's first turn) sees what fired:

```
/startup complete: orphan-check (N tasks recovered, M archived), schedules (K crons + watcher).
```

The orphan-check fields say `skipped (skill not installed)` if step 1 was skipped.

## Sequence diagram

```
session start
    │
    ▼
/startup
    │
    ├─► step 1:  /task-orphan-check (optional) ──► classifies + archives orphan tasks
    │
    ├─► step 2:  /schedule-crons ──┬─► step 1-3 (register crons.json entries)
    │                               ├─► step 4 (proactive-loop fallback if missing)
    │                               ├─► step 5 (start watch-tasks-stream.sh via Monitor)
    │                               └─► step 6 (confirm what was scheduled)
    │
    └─► step 3: emit summary
```

## Re-invoking in an already-running session

If `/startup` is invoked mid-session, the sub-skills skip their already-done work (an already-scheduled cron isn't re-created, an already-running watcher isn't restarted), so the result is effectively a re-confirm of state. Safe.

## What lives elsewhere

This skill is intentionally a thin orchestrator. Logic lives in the sub-skills:

- **Orphan recovery**: `skills/task-orphan-check/` (separate PR, optional)
- **Cron registration + watcher start**: `skills/schedule-crons/`

If you find yourself wanting to put logic IN `/startup`, ask whether it belongs in one of the sub-skills (or a new sub-skill) first. `/startup` is the order, not the work.

## Iteration log

- v0.1.0 — 2026-05-23 — initial draft. Per Chi 2026-05-23 Discord exchange about #1049 redesign ("make a new skill and include everything we need at start"). `/startup` becomes the canonical CLI entry; `/schedule-crons` remains callable for manual cron re-registration. Migration: launchd plists + CLI scripts switch to `/startup`.
- v0.2.0 — 2026-06-21 — removed the fresh-session briefing step and its session sentinel (that sub-skill was deleted). `/startup` now runs orphan-check → schedules + watcher → confirm; sub-skill idempotency replaces the former sentinel guard.
