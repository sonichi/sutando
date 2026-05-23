---
name: startup
description: "Single entry point for fresh-session bootstrap. Runs catchup, optional task-orphan recovery, cron registration, and watcher start in a fixed order. Replaces the current `claude -- '/schedule-crons'` invocation pattern as the canonical CLI startup target."
user-invocable: true
---

# Startup

The canonical entry point for a fresh Sutando session. Bundles every action that must happen once at session start, in the correct order.

**Usage**: `/startup`

ARGUMENTS: $ARGUMENTS (currently unused — reserved for future per-instance overrides)

## What this replaces

Previously: `claude -- "/schedule-crons"` was the de-facto startup invocation, and `skills/schedule-crons/SKILL.md` accumulated startup ceremony (step 0 catchup, step 4 cron-fallback, step 5 watcher) on top of its actual job (registering crons from `crons.json`).

Now: `claude -- "/startup"` is the canonical startup target. `/startup` orchestrates the sequence; `/schedule-crons` shrinks back to its narrow job.

Migration: update `~/Library/LaunchAgents/*.plist` and any CLI invocation scripts to call `/startup` instead of `/schedule-crons`. `/schedule-crons` still works standalone (for manual cron re-registration) — both paths cooperate idempotently via the same sentinel guard.

## Why one bundled skill

Per Chi 2026-05-23 Discord: "we can make a new skill and include everything we need at start." Five rationales:

1. **Single entry point** — no more "which skill does the CLI invoke?" The launchd plist points at `/startup` and only at `/startup`.
2. **Ordering encoded in one place** — the sequence (read state → recover state → register schedules → start watcher) lives in this skill's `On Activation` section, not scattered across schedule-crons's step list.
3. **Easy to extend** — future startup work (new lifecycle checks, telemetry pings, dependency probes) appends to this skill's sequence; no debate about where it belongs.
4. **Each sub-step stays callable standalone** — `/catchup-after-startup`, `/schedule-crons`, etc. continue to work for manual invocation. `/startup` is a wrapper, not a replacement.
5. **Idempotent re-invocation** — calling `/startup` twice in the same session is safe; the sentinel guard inside each sub-skill makes the second call a no-op.

## On Activation

The sequence below MUST run in this order. Each step is sentinel-guarded or naturally idempotent, so re-invocation is safe.

### Step 1 — Catchup briefing

Invoke `/catchup-after-startup` (shipped in #1056). Reads `session-state.md`, previous-session JSONL, open PRs, in-flight tasks, recent results, pending questions, recent voice/phone/discord activity, recent chat, recent commits, build_log tail, health-check one-liner. The output lands in the conversation buffer so the rest of `/startup` (and the agent's first real turn) has cross-restart context.

Skip if: the `proactive-loop-started.sentinel` is already present (indicates this is a cron-driven re-invocation within an already-running session, not a fresh start).

### Step 2 — Task orphan check (optional, sentinel-guarded)

Invoke `/task-orphan-check` IF the skill is installed (i.e. `~/.claude/skills/task-orphan-check/` exists). This is the recovery half of the post-#1049 redesign: scan `<workspace>/tasks/` for orphan tasks left over from a crash mid-execution, cross-reference per-side-effect markers (e.g. PR #1048's `.sending` files), archive completed tasks, write recovery sentinels for stuck ones. See the skill itself for the full procedure.

If the skill is not installed, skip silently. `/startup` works without it — every other step is independent.

Note: this step runs BEFORE step 3 so that the watcher (started by step 3's downstream) doesn't pick up an orphan task before recovery has classified it.

### Step 3 — Register schedules + start watcher

Invoke `/schedule-crons`. This handles:
- Reading `skills/schedule-crons/crons.json`
- Calling `CronCreate` for each entry that isn't already scheduled
- Ensuring a fallback `/proactive-loop` cron exists at `*/10 * * * *` if `crons.json` doesn't include one (post-#954 belt-and-suspenders)
- Starting the streaming task watcher via the `Monitor` tool (`bash src/watch-tasks-stream.sh`, persistent, description `"Streaming task watcher"`)

`/schedule-crons` step 0 (its internal catchup invocation) is now a no-op because step 1 of `/startup` already touched the sentinel. Symmetric idempotency.

### Step 4 — Touch the sentinel

After all steps complete, ensure `<workspace>/state/proactive-loop-started.sentinel` exists:

```bash
mkdir -p "${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/state"
touch "${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/state/proactive-loop-started.sentinel"
```

This makes future cron-driven invocations of `/schedule-crons` (or `/proactive-loop`) skip their catchup step. The sentinel is cleared by the `SessionStop` hook in `src/session-handoff.sh` so the next fresh session re-runs catchup. (Symmetric with the existing logic in `/catchup-after-startup`'s install-hook.)

### Step 5 — Confirm

Emit a one-line summary so the operator (or main session's first turn) sees what fired:

```
/startup complete: catchup (briefing read), orphan-check (N tasks recovered, M archived), schedules (K crons + watcher), sentinel touched.
```

The orphan-check fields say `skipped (skill not installed)` if step 2 was skipped.

## Sequence diagram

```
session start
    │
    ▼
/startup
    │
    ├─► step 1: /catchup-after-startup ──► reads briefing into conversation
    │
    ├─► step 2: /task-orphan-check (optional) ──► classifies + archives orphan tasks
    │
    ├─► step 3: /schedule-crons ──┬─► step 0 (no-op, sentinel touched)
    │                              ├─► step 1-3 (register crons.json entries)
    │                              ├─► step 4 (proactive-loop fallback if missing)
    │                              ├─► step 5 (start watch-tasks-stream.sh via Monitor)
    │                              └─► step 6 (confirm what was scheduled)
    │
    ├─► step 4: touch sentinel
    │
    └─► step 5: emit summary
```

## Skipping when invoked in an already-running session

If `/startup` is invoked when the sentinel already exists (someone manually typed it mid-session), the sub-skills will skip their first-time-only work and the result is effectively a re-confirm of state. Safe; the operator may see "(skipped — sentinel present)" notes from each sub-skill.

To force a full re-startup: `rm $SUTANDO_WORKSPACE/state/proactive-loop-started.sentinel` then `/startup`.

## What lives elsewhere

This skill is intentionally a thin orchestrator. Logic lives in the sub-skills:

- **Catchup briefing**: `skills/catchup-after-startup/` (#1056)
- **Orphan recovery**: `skills/task-orphan-check/` (separate PR, optional)
- **Cron registration + watcher start**: `skills/schedule-crons/`
- **Sentinel clear on session end**: `src/session-handoff.sh` (already in place via #1056)

If you find yourself wanting to put logic IN `/startup`, ask whether it belongs in one of the sub-skills (or a new sub-skill) first. `/startup` is the order, not the work.

## Iteration log

- v0.1.0 — 2026-05-23 — initial draft. Per Chi 2026-05-23 Discord exchange about #1049 redesign ("make a new skill and include everything we need at start"). Bundles existing `/catchup-after-startup` (#1056) + the still-unfinished `/task-orphan-check` (separate PR) + the existing `/schedule-crons`. `/startup` becomes the canonical CLI entry; `/schedule-crons` remains callable for manual cron re-registration. Migration: launchd plists + CLI scripts switch to `/startup`.
