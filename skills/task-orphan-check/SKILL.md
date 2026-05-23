---
name: task-orphan-check
description: "Resolve orphan tasks left in `<workspace>/tasks/` from a previous session that crashed mid-execution. Classifies each live task as done / fresh / stale by cross-referencing per-side-effect markers, then archives or recovers as appropriate. Runs once on startup; safe to re-invoke."
user-invocable: true
---

# Task orphan check

Recovery half of the post-#1049 task-bridge redesign. Replaces the brittle attempts-counter (#1049 + #1066's followup) with a startup-time classification pass that uses existing side-effect markers (PR #1048's `.sending` files for Discord, result files in `results/`, archive presence) to decide what to do with each live task in `<workspace>/tasks/`.

**Usage**: `/task-orphan-check`

Designed to be invoked from `/startup` (PR #1072) as step 2, before `/schedule-crons` starts the task watcher. Also callable standalone for manual recovery.

## Why this exists

If the agent crashes mid-task with non-idempotent side effects already executed (Discord message sent, file written, API call made) but the archive of result + task files never ran, on restart the task file is still in `tasks/`. The watcher re-emits it. The agent re-processes. The side effect fires a second time.

PR #1049 tried to solve this with an `attempts: N` counter inside the task file — but the bumper-write fired the watcher's own `Renamed` event, creating an infinite self-trigger loop. PR #1066 tried to patch the loop by switching to in-place writes — but on macOS, `open(file, 'w')` STILL fires the `Created` event because `O_WRONLY|O_CREAT|O_TRUNC` flips the ItemCreated bit. Both PRs are working around the wrong layer.

This skill moves the dedup logic out of the watcher's event surface entirely. The agent does a single classification pass at startup, cross-references markers that already exist (PR #1048 ships them for Discord delivery; result files in `results/` mark "this task was completed"), and decides per-task what to do. No counter, no in-band writes, no self-trigger loop.

## On Activation

The procedure below is non-LLM where possible — mechanical file checks + side-effect marker reads. The LLM-judgment parts are bounded (per-task classification with explicit decision rules).

### Step 1 — List live tasks

```bash
WS="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
ls "$WS/tasks/"task-*.txt 2>/dev/null | head -200
```

If no live tasks, emit "orphan-check: no live tasks, nothing to recover" and idle.

### Step 2 — Classify each task

For each `tasks/task-<id>.txt`:

1. **Parse the header** — extract `id`, `timestamp`, `source`, `channel_id` (if Discord), `user_id`.

2. **Cross-reference completion markers** (any single match = task already completed):
   - **`<workspace>/results/task-<id>.txt`** exists → DONE (the result file is the canonical completion marker; if it exists the task was processed)
   - **`<workspace>/results/archive/task-<id>.txt`** exists → DONE (post-archive case)
   - **`<workspace>/results/proactive-<task-id>.txt`** OR `.sending` variant exists → DELIVERY IN PROGRESS / DONE (PR #1048's idempotency sentinel)

3. **Compute age**:
   - `task_age_s = now - mtime(tasks/<id>.txt)`
   - If <300s (5 min) → FRESH (genuinely just arrived; watcher will pick it up normally)
   - Else → ORPHAN (no completion marker AND old enough to be from a previous session)

4. **Classify outcome**:
   - **DONE** → archive the task file: `mv tasks/task-<id>.txt tasks/archive/task-<id>.txt`. Log: `done: completion marker found at <path>`.
   - **FRESH** → leave alone. Log: `fresh: arrived <N>s ago, watcher will handle`.
   - **ORPHAN** → write a recovery result: see step 3.

### Step 3 — Recover orphan tasks

For each ORPHAN task, write a sentinel result so the bridge delivers a "needs review" note to the original sender, then archive the task file:

```
<workspace>/results/task-<id>.txt:

Orphan recovery: this task arrived <N>m ago and was not completed before the previous session ended.

Original task body preserved below — review before re-queuing if it has non-idempotent side effects (DM sent, file written, API call). To re-queue: move from tasks/archive/task-<id>.txt back to tasks/task-<id>.txt.

---
<original task body verbatim>
```

Then `mv tasks/task-<id>.txt tasks/archive/task-<id>.txt`. The bridge reads the result + delivers the recovery note + archives. Log: `recovered: stuck for <N>m, sentinel result written`.

### Step 4 — Sanity check archive directory

Confirm `tasks/archive/` exists; create if not (`mkdir -p`). Should always be present in normal operation; defensive.

### Step 5 — Emit summary

```
orphan-check complete:
  total live tasks scanned: N
  archived as done (completion marker found): M
  left fresh for watcher: K
  recovered as orphan (sentinel result written): J
```

The summary lands in the conversation buffer so the agent's first turn (and operator) sees what happened. If `M+K+J ≠ N`, the script bailed mid-pass — log a warning and let the operator investigate.

## What this DOES NOT touch

- `<workspace>/tasks/archive/` — graveyard; never modified except by this skill's own archive moves.
- The watcher (`watch-tasks-stream.sh`) — runs unchanged; just sees a smaller `tasks/` dir after orphan-check completes.
- The bridges (`discord-bridge.py`, `telegram-bridge.py`) — orphan-check reads their per-side-effect markers (`.sending` files from #1048) but never modifies them.
- `crons.json` or any scheduler state.
- Memory dir or `MEMORY.md`.

## What it MIGHT need in the future

- **More side-effect markers**: PR #1048 ships Discord-delivery idempotency markers. As other tools/bridges get similar markers (Telegram delivery, file-write logs, API-call IDs), orphan-check should learn to read them at step 2. Listed under "What it does not cover" in step 2 above is a known gap — currently we cover Discord + result-file presence; voice / phone / Telegram side effects are harder to detect cleanly. Conservative path: any orphan without a CLEAR completion marker gets the recovery-sentinel treatment, which surfaces to the operator rather than silently re-firing.

## Failure modes

- **Workspace dir missing** — emit "orphan-check: workspace not found at $WS, skipping" and idle. Don't fail the rest of `/startup`.
- **`tasks/` dir missing** — emit "orphan-check: no tasks/ dir, nothing to recover" (fresh workspace) and idle.
- **Task file unparsable** — log warning, treat as ORPHAN (conservative — surface to operator).
- **Result file write fails** — log error, leave task file untouched, surface in summary.

## Why not just clear `tasks/` at startup?

That would lose tasks that legitimately arrived in the gap between previous session's death and this session's startup. Those need to be processed, not nuked. The classification pass distinguishes "completed but unarchived" from "arrived and never seen."

## Relationship to other PRs

- **#1048 (merged)** — VasiliyRad's Discord delivery-idempotency sentinel. The `.sending` files orphan-check reads at step 2 come from this PR. Keeps.
- **#1049 (merged)** — VasiliyRad's attempts-counter. Becomes redundant with this skill. Recommend revert: drop `task_bump_attempts.py`, remove watcher's bump-on-emit hook, drop the `attempts:` field from task file format (back-compat: agents can ignore the field if present in older task files).
- **#1056 (merged)** — Lucy's catchup-after-startup. Complementary: catchup READS state (briefing); orphan-check MUTATES state (archives + recovers). Both fire from `/startup` step 1 and step 2 respectively.
- **#1066 (still open as of skill draft)** — VasiliyRad's bumper in-place-write fix. Becomes moot if #1049 is reverted. Recommend close as "superseded by /task-orphan-check."
- **#1072 (this PR's sibling)** — `/startup` skill. Invokes `/task-orphan-check` as step 2 if installed.

## Implementation note: this skill ships SKILL.md only

For now, the skill is markdown — the agent reads the procedure above and executes it via Read + Bash + Write tool calls. No `scripts/orphan-check.sh` because:

1. The classification rules are LLM-judgment territory (cross-reference multiple markers, compute age relative to "now," decide between three outcomes).
2. A bash script would re-implement what the agent does natively, adding a separate code path to test + maintain.
3. The work is small per-pass (typically 0-3 live tasks; rarely >10 even after a long crash).

If the workload grows or we want deterministic testing, a `scripts/orphan-check.py` mirror is the natural next step.

## Iteration log

- v0.1.0 — 2026-05-23 — initial draft. Per Chi 2026-05-23 Discord exchange about #1049 redesign ("simply ask the agent to check when starting"). Designed to be invoked from `/startup` step 2 (PR #1072). Standalone-callable for manual recovery. Replaces the attempts-counter approach (#1049 + #1066's followup) with a startup-time classification using existing side-effect markers (#1048's `.sending` files + result-file presence). No bumper, no in-band writes, no self-trigger loop.
