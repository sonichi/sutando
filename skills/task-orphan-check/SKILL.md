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

For each file in `tasks/`, let `<id>` be the value of the `id:` header line (e.g. `task-1779570142563`). The file is `tasks/<id>.txt`. Per-task paths below use `<id>` consistently — note `<id>` already includes the `task-` prefix; do NOT add it again.

1. **Parse the header** — extract `id`, `timestamp`, `source`, `channel_id` (if Discord), `user_id`, `access_tier` (`owner` / `team` / `other`; default to `owner` if the field is absent — pre-tier task files predate the field and were authored by the owner).

2. **Cross-reference completion markers** (any single match = task already completed):
   - **`<workspace>/results/<id>.txt`** exists → **DONE**. The result file is the canonical completion marker; if it exists the task was processed.
   - **`<workspace>/results/archive/<id>.txt`** exists → **DONE** (post-archive case).
   - **`<workspace>/results/proactive-<id>.txt`** OR `.sending` variant exists → see step 2b below for the in-progress-vs-done split.

   **Step 2b — `.sending` contract clarification** (per qingyun-sutando review of #1074):
   - `results/<id>.txt` (no suffix) → task completed AND result body written. **DONE.**
   - `results/<id>.txt.sending` → the discord-bridge picked the result up and is mid-delivery (per #1046/#1048's lifecycle). Treat as **DONE** for orphan-check purposes — the bridge already owns post-crash recovery for these via its own startup `.sending` sweep, so we don't second-guess. Read-only either way.
   - `results/proactive-<id>.txt[.sending]` → same pattern for proactive DMs.

3. **Compute age** — use the IMMUTABLE arrival time, NOT file mtime (mtime gets reset by rsync, `git checkout`, `touch`, or workspace sync, which would make a genuinely old orphan look FRESH and re-fire its side effect — exactly the bug this skill exists to prevent):
   - Preferred: parse the header `timestamp:` ISO field → `task_age_s = now - parse(timestamp)`.
   - Fallback: extract epoch-ms from the id (id format is `task-<epoch-ms>`) → `task_age_s = now - (epoch_ms/1000)`.
   - Last resort only if both unparseable: `task_age_s = now - mtime(tasks/<id>.txt)`.
   - If <300s (5 min) → FRESH (genuinely just arrived; watcher will pick it up normally).
   - Else → ORPHAN (no completion marker AND old enough to be from a previous session).

4. **Classify outcome**:
   - **DONE** → archive the task file: `mv tasks/<id>.txt tasks/archive/<id>.txt`. Log: `done: completion marker found at <path>`.
   - **FRESH** → leave alone. Log: `fresh: arrived <N>s ago, watcher will handle`.
   - **ORPHAN** → write a recovery result: see step 3.

### Step 3 — Recover orphan tasks (tier-aware)

ORPHAN handling depends on `source` because text-side recovery only makes sense for surfaces where the owner can still see and act on the DM. Voice/phone conversations don't replay in text. The flat per-task sentinel that v0.1.1 used was the wrong default for high-volume team-tier orphans whose conversation threads had moved on — see `feedback_orphan_check_tier_classify_before_sentinel` for the 2026-05-26 post-mortem (22-message blast across 5 channels, 13 of them into one active episode thread).

**Invocation contract.** orphan-check runs once at `/startup` step 2 (PR #1072), BEFORE the task watcher attaches. Under that contract the deferred branch (row 2 of the table below) has no race window — the watcher isn't running between step 3's defer-decision and step 3b's archive mv. If you invoke `/task-orphan-check` standalone with a live watcher already attached, the deferred branch is racy: the watcher may pick up the un-archived `tasks/<id>.txt` between step 3 and step 3b and re-classify it as FRESH or ORPHAN on its own pass (no `results/<id>.txt` exists yet, so the completion-marker check in step 2.2 won't catch it). Safe workaround for standalone invocation: rename `tasks/<id>.txt` → `tasks/<id>.txt.deferred` at defer-time so the watcher's `task-*.txt` glob skips it, then have step 3b mv `tasks/<id>.txt.deferred` → `tasks/archive/<id>.txt` atomically. Verify the watcher's glob actually excludes the suffix on your installation before relying on it.

**Decision table — apply per orphan, first match wins:**

| `source` | Action |
|----------|--------|
| `voice` / `phone` (any tier) | **Silent archive.** Text recovery to a voice/phone surface is the wrong shape; the conversation has hung up or moved on. `mv tasks/<id>.txt tasks/archive/<id>.txt`. No result write. Log: `archived-silent: voice/phone source`. |
| any other source (incl. `discord` / `telegram` / `slack` / `chat` / `whatsapp` / `email` / missing field / future surfaces) | **Defer; aggregate in step 3b.** Append `<id>` to an in-pass `deferred_orphans` list (along with its `access_tier` for per-tier counting). Do NOT write a per-task result. Do NOT archive yet — leave the task file in `tasks/` so step 3b consumes it (see "Invocation contract" above for the standalone-invocation `.deferred` suffix workaround). Log: `deferred: queued for consolidated DM (source=<source>, tier=<tier>)`. |

**All tiers (owner / team / other) flow through the same aggregated DM** so the owner has visibility into stale tasks across all tiers — previously, team/other-tier orphans were `[no-send]`-archived and the owner had no record. Preview-extraction in step 3b strips the bridge-injected system-instructions block so non-owner previews show the actual user ask, not boilerplate.

#### Step 3b — Aggregate deferred orphans into ONE proactive DM

Run once at the end of the orphan pass, after every orphan has been classified by the table above. If `deferred_orphans` is empty, skip.

Otherwise:

1. `ts=$(date +%s)`.

2. **Extract preview body for each orphan, stripping any in-band system-instructions block.** Non-owner-tier task files have a `===SUTANDO SYSTEM INSTRUCTIONS===` block injected at the FRONT by the bridge (`src/discord-bridge.py`). Previewing the first 100 chars without stripping would leak the boilerplate, not the user's actual ask. For each `<id>`:

   ```python
   body = read("tasks/<id>.txt")
   if "===SUTANDO SYSTEM INSTRUCTIONS===" in body:
       after_first = body.split("===SUTANDO SYSTEM INSTRUCTIONS===", 1)[1]
       if "===" in after_first:
           body = after_first.split("===", 1)[1]
   preview = body.strip()[:100]
   ```

   The system-instructions block is only **stripped for the preview** — the archived task file body remains intact (see step 5), so re-queueing via `mv tasks/archive/<id>.txt tasks/` preserves sandboxing for non-owner tiers.

3. Group `deferred_orphans` by `access_tier` (owner / team / other) → per-tier counts; and by `channel_name` → per-channel counts.

4. Apply step 3c bomb-guard (see below) to decide whether to truncate the preview list.

5. Write `<workspace>/results/proactive-orphan-recovery-${ts}.txt`:

   ```
   Orphan recovery — N stale tasks from a prior session (oldest <Nm>, newest <Nm>, no completion markers).

   By tier: owner (<o>), team (<t>), other (<r>).
   By channel: <ch1> (<x>), <ch2> (<y>), DM (<z>), ...

   Previews (most-recent first, first ~100 chars of task body; in-band system instructions stripped):
   - task-<id> [<tier>, <channel>, <Nm ago>]: <preview>
   - ...
   [If truncated by step 3c: "+<N-20> more — see tasks/archive/ for the full list."]

   To re-queue an individual task: `mv $SUTANDO_WORKSPACE/tasks/archive/task-<id>.txt $SUTANDO_WORKSPACE/tasks/`
   The archived file retains its original body (incl. system-instructions block for non-owner tasks), so re-queueing preserves sandboxing.
   If none still matter: no action needed — they're already archived.
   ```

6. For each `<id>` in `deferred_orphans`: `mv tasks/<id>.txt tasks/archive/<id>.txt`.

The bridge routes `proactive-*` to the owner's DM (single delivery), not back to each origin channel. Log: `aggregated-all-tiers: <N> orphans → 1 proactive DM (owner=<o>, team=<t>, other=<r>)`.

#### Step 3c — Bomb-guard (defense in depth)

Two layers:

1. **Total-deferred-count cap.** If `len(deferred_orphans) > 30`, truncate the preview list in step 3b's DM body to the 20 most-recent orphans and add a footer: `+<N-20> more — see tasks/archive/ for the full list.` The per-tier and per-channel count lines remain accurate (they reflect the full set, not the truncated preview list). This bounds DM size when a long crash (multi-day) leaves dozens of stale tasks.

2. **Per-channel-delivery guard (future-proofing).** If any future code path adds per-channel result writes (e.g., a 3rd table row that bypasses both silent-archive and the aggregated DM), tally per-channel deliveries; collapse any single `channel_id` receiving >5 into one summary post. Today the table emits zero per-channel posts (voice silent, everything else aggregated into one owner DM), so this branch is a no-op — defense so the next person who adds a 3rd row can't accidentally re-create the v0.1.1 noise-bomb.

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

## Known residual risk: the <5min sub-window for non-Discord surfaces

A task that arrived <5 minutes before a crash, executed its side effect, then died before writing its result file has NO completion marker AND looks FRESH (age < 5min) → orphan-check leaves it for the watcher → side effect re-fires. Unavoidable without per-side-effect markers, and the `.sending` markers only close it for Discord.

**Currently covered:** Discord DM delivery (PR #1048's `.sending`), file presence in `results/`.

**Residual hole, in priority order:**
- Voice agent side effects (no marker file yet).
- Phone-call agent side effects (same).
- Telegram delivery (Telegram bridge doesn't yet ship a `.sending` analog of #1048).
- Generic API calls / shell mutations without their own marker file.

Conservative default for the hole: any orphan without a CLEAR completion marker gets the recovery-sentinel treatment, which surfaces to the operator rather than silently re-firing. As other bridges/tools grow their own per-side-effect markers, orphan-check should learn to read them at step 2 — the marker list is intentionally a code-level data table, not buried in prose.

## What it MIGHT need in the future

- **More side-effect markers** (see "Known residual risk" above): voice/phone/Telegram especially.
- **Promote to a deterministic script** (`scripts/orphan-check.py` mirror) once the marker set + age rules are stable enough that unit tests buy more than they cost. The current SKILL-only ship trades testability for being one less code-path to maintain; flip if/when the rules grow past "marker-or-not + age-vs-5min".

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
- v0.1.1 — 2026-05-23 — qingyun-sutando review pass. **(1)** Fixed `<id>` ambiguity — `<id>` is the value of the `id:` header (already includes `task-` prefix); paths are `results/<id>.txt` NOT `results/task-<id>.txt` (the prior wording double-prefixed and would have misclassified every completed-but-unarchived task as ORPHAN → spurious recovery notes). **(2)** Age now derives from immutable header `timestamp:` / `task-<epoch-ms>` id, NOT file mtime (mtime resets on rsync / `git checkout` / `touch` / workspace sync, making old orphans look FRESH → re-fire). **(3)** Clarified `.sending` contract via new step 2b: `<id>.txt` (no suffix) = DONE, `<id>.txt.sending` = bridge mid-delivery (treat as DONE; bridge owns its own crash recovery via #1046/#1048's startup sweep). **(4)** Named the <5min residual hole explicitly under its own section, with prioritized coverage list (voice / phone / Telegram + generic API). **(5)** Noted "promote to scripts/orphan-check.py" trigger.
- v0.1.2 — 2026-05-26 — tier-aware orphan recovery. Step 2.1 now parses `access_tier:` (default `owner` for legacy task files lacking the field). Step 3 rewritten as a decision table branching on `source` + `access_tier`: voice/phone → silent archive; team/other → `[no-send]` archive; owner discord/telegram/slack/chat → defer to new step 3b (consolidated proactive DM aggregating all owner-tier orphans this pass into ONE `proactive-orphan-recovery-<ts>.txt` instead of N per-channel sentinels). New step 3c bomb-guard collapses any future >5-deliveries-to-one-channel into a single summary post (no-op today; defense for future branch additions). Triggered by 2026-05-26 noise-bomb post-mortem (`feedback_orphan_check_tier_classify_before_sentinel`): v0.1.1 sentinel-blasted 22 stale tasks across #ep013 (13), #talk (4), voice channels (4), and DM (1) — wrong default for high-volume team-tier orphans whose threads had moved on, and wrong shape (N per-channel posts) for owner-tier ones. Sibling work on cross-fleet bridges (qingyun-sutando MacBook branch) adds defensive bot-user_id tier-filter so peer bots' stale tasks don't tier as `owner` via allowFrom inheritance — that's the upstream cause of the same skill running on a sibling fleet seeing 21/22 of one fleet's orphans as `owner` rather than `team`.
- v0.1.3 — 2026-05-26 — liususan091219 (Maddy / MBP node) review pass on PR #1241. **(1)** Row 3's `source` column was an enumeration (`discord / telegram / slack / chat / unknown`), which under literal reading meant a task with `source: whatsapp` (or any future surface) + `access_tier: owner` matched no row and wedged in `tasks/` forever. Rewritten as a true catch-all (`any source not matched by row 1`). **(2)** Added explicit "Invocation contract" paragraph at the top of Step 3 documenting the assumed-no-race-window guarantee (orphan-check runs at `/startup` step 2 before the watcher attaches), plus a standalone-invocation workaround (`tasks/<id>.txt.deferred` suffix so the watcher's `task-*.txt` glob skips deferred-owner files between step 3 and step 3b). Sibling PR #1233 (bridge-side bot-sender tier-downgrade) closed at owner request 2026-05-27 01:44Z; this PR now standalone.
- v0.1.4 — 2026-05-27 — per Chi 17:50Z Discord. Decision table collapsed from 3 rows to 2 (voice/phone silent + everything-else aggregated). **Team/other-tier orphans now flow into the same proactive DM as owner-tier** (was: `[no-send]` archive, owner had zero visibility); aggregated DM gains a `By tier:` line so the tier-mix is scannable. Step 3b adds explicit preview-extraction that strips the bridge-injected `===SUTANDO SYSTEM INSTRUCTIONS===` block before slicing the first 100 chars — non-owner task bodies put the block at the FRONT, so unprocessed previews would have leaked boilerplate, not user content. Step 3c bomb-guard restructured: total-deferred-count cap (truncate preview list to 20 + "+X more" footer when >30 deferred) becomes layer 1; per-channel-delivery cap demoted to layer 2 (future-proofing no-op today). The system-instructions block is **preserved in the archived task file body** — only the preview-in-DM strips it. Re-queueing via `mv tasks/archive/<id>.txt tasks/` preserves sandboxing for non-owner tiers.
