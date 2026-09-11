---
name: task-orphan-check
description: "Resolve orphan tasks left in `<workspace>/tasks/` from a previous session that crashed mid-execution. Classifies each live task as done / fresh / stale by cross-referencing per-side-effect markers, then archives or recovers as appropriate. Runs once on startup; safe to re-invoke."
user-invocable: true
---

# Task orphan check

Recovery half of the post-#1049 task-bridge redesign. Replaces the brittle attempts-counter (#1049 + #1066's followup) with a startup-time classification pass that uses existing side-effect markers (PR #1048's `.sending` files for Discord, result files in `results/`, archive presence) to decide what to do with each live task in `<workspace>/tasks/`.

**Usage**: `/task-orphan-check`

Designed to be invoked from `/startup` (PR #1072) as step 1, before `/schedule-crons` starts the task watcher. Also callable standalone for manual recovery.

## Why this exists

If the agent crashes mid-task with non-idempotent side effects already executed (Discord message sent, file written, API call made) but the archive of result + task files never ran, on restart the task file is still in `tasks/`. The watcher re-emits it. The agent re-processes. The side effect fires a second time.

PR #1049 tried to solve this with an `attempts: N` counter inside the task file — but the bumper-write fired the watcher's own `Renamed` event, creating an infinite self-trigger loop. PR #1066 tried to patch the loop by switching to in-place writes — but on macOS, `open(file, 'w')` STILL fires the `Created` event because `O_WRONLY|O_CREAT|O_TRUNC` flips the ItemCreated bit. Both PRs are working around the wrong layer.

This skill moves the dedup logic out of the watcher's event surface entirely. The agent does a single classification pass at startup, cross-references markers that already exist (PR #1048 ships them for Discord delivery; result files in `results/` mark "this task was completed"), and decides per-task what to do. No counter, no in-band writes, no self-trigger loop.

## On Activation

The procedure below is non-LLM where possible — mechanical file checks + side-effect marker reads. The LLM-judgment parts are bounded (per-task classification with explicit decision rules).

### Step 1 — List live tasks

```bash
WS="$(bash scripts/sutando-config.sh workspace)"
ls "$WS/tasks/"task-*.txt 2>/dev/null | head -200
```

If no live tasks, emit "orphan-check: no live tasks, nothing to recover" and idle.

### Step 2 — Classify each task

**Run the classifier; do not re-derive the verdicts by hand:**

```bash
python3 skills/task-orphan-check/scripts/classify.py        # add --workspace DIR to override
```

It prints one JSON object — `tasks[]` with `id`, `source`, `access_tier`, `channel_id`, `label` (the readable channel label step 3b uses), `age_s`, `import` (with `import_intent`; `import_task_id` — the `task_id` the current `status.json` carries, `null` when it has none; and for a bound or unbound run `import_phase` / `import_idle_s`), `verdict` (`done` / `fresh` / `orphan` / `import-resume` / `import-stalled` / `import-unbound`) and a `reason` naming the marker or age line it matched — plus `counts`. Read-only: it moves and writes nothing; step 3 acts on its verdicts. The rules it encodes are the ones below (pinned by `tests/task-orphan-check-classify.test.py`); if you change a rule, change the script and its test, then this prose.

For each file in `tasks/`, let `<id>` be the value of the `id:` header line (e.g. `task-1779570142563`). The file is `tasks/<id>.txt`. Per-task paths below use `<id>` consistently — note `<id>` already includes the `task-` prefix; do NOT add it again.

1. **Parse the header** — extract `id`, `timestamp`, `source`, `channel_id` (Discord channel or ag2.space room id — both producers supply it, and the later `contextNotFrom`/requeue workflow needs the exact id, so never discard it in favor of the friendly name), `chat_id` (Telegram), `room_name` / `channel_name` (whichever the surface carries — ag2.space sends `room_name` and no `channel_name`, so a reader that parses only the latter gets nothing and step 3b falls back to the raw id), `user_id`, `access_tier` (`owner` / `team` / `other`; default to `owner` if the field is absent — pre-tier task files predate the field and were authored by the owner). The classifier reads them with `local_task_protocol.parse_task_headers_lenient` — the shape-union parser — because this pass sees files from every writer and era: the desktop's legacy Claude-import writer is task-mid (`task:` before `channel_id:`), and the strict task-last parser would read its `channel_id` and `priority` as absent.

2. **Cross-reference completion markers** (any single match = task already completed):
   - **`<workspace>/results/<id>.txt`** exists → **DONE**. The result file is the canonical completion marker; if it exists the task was processed.
   - **`<workspace>/results/archive/<id>.txt`** exists → **DONE** (post-archive case).
   - **`<workspace>/results/proactive-<id>.txt`** OR `.sending` variant exists → see step 2b below for the in-progress-vs-done split.
   - **`<workspace>/deliveries/<worker>/<id>.accepted`** (or `.claimed`) exists → **DELIVERED**: the route handler handed the task to a pool worker and the worker accepted it. It is in flight there, and the worker's finish archives the task file, so this pass leaves it in `tasks/`, never archives it and never re-queues it, whatever its age. Count it in the summary; do not list it in the recovery DM. (2026-09-11: three owner asks accepted by a worker were archived by this pass and re-queued, so the owner got two answers each.)

   **Step 2b — `.sending` contract clarification** (per qingyun-sutando review of #1074):
   - `results/<id>.txt` (no suffix) → task completed AND result body written. **DONE.**
   - `results/proactive-<id>.txt[.sending]` → the bridge claimed the proactive DM by rename and is mid-delivery. Treat as **DONE** for orphan-check purposes — the bridge owns post-crash recovery via its own startup `.sending` sweep, so we don't second-guess. Read-only either way.
   - **`results/<id>.txt.sending` (a TASK result) does not occur — do not classify on it.** Every claim-by-rename site gates on the proactive family *before* applying the suffix, so no `task-*` result is ever renamed:

     | site | gate applied before `.sending` |
     |---|---|
     | `src/discord-bridge.py` claim loop | `f.name.startswith("proactive-")` |
     | `src/slack-bridge.py` claim loop | `f.name.startswith("proactive-")` |
     | `src/telegram-bridge.py` claim loop | `PROACTIVE_PREFIXES` = `("proactive-", "briefing-", "insight-", "friction-")` |

     This line previously described the task form as a live mid-delivery state. It is a dead branch, and not a harmless one: on 2026-08-02 it was cited as a real completion namespace while reviewing #2525, which would have added handling for a case that cannot arise. `tests/sending-suffix-is-proactive-only.test.py` pins the invariant; if a future change *does* start claiming task results by rename, that test fails and this row must be restored **with the producing site named**.

3. **Compute age** — use the IMMUTABLE arrival time, NOT file mtime (mtime gets reset by rsync, `git checkout`, `touch`, or workspace sync, which would make a genuinely old orphan look FRESH and re-fire its side effect — exactly the bug this skill exists to prevent):
   - Preferred: parse the header `timestamp:` ISO field → `task_age_s = now - parse(timestamp)`.
   - Fallback: extract epoch-ms from the id (id format is `task-<epoch-ms>`) → `task_age_s = now - (epoch_ms/1000)`.
   - Last resort only if both unparseable: `task_age_s = now - mtime(tasks/<id>.txt)`.
   - If <300s (5 min) → FRESH (genuinely just arrived; watcher will pick it up normally).
   - Else → ORPHAN (no completion marker AND old enough to be from a previous session).

   **Step 3a — consented Claude-Code import tasks are not orphans at 5 minutes.** A task is an *import task* when it is owner-tier and carries a **run intent**: the header `channel_id: onboarding-wizard` (the desktop's legacy Import-button writer, `claude_import.rs`); the slash command `/import-claude-context` as a standalone token; or one of the documented trigger sentences, case-insensitive — "import my Claude history", "import my Claude Code history" (the onboarding DM message and the Settings sentence both contain it), "read my Claude Code sessions", "bring my Claude context along". A path or a bare skill-name mention is **not** an intent: an owner review task reading "Review PR 4177 which touches skills/import-claude-context/SKILL.md" classifies like any other task (orphan at 5 min, archived with a re-queue line) — the first cut matched the bare substring and would have parked that task in `tasks/` forever, invisible to the recovery DM (#4177 review). The import is consented, idempotent and resumable (`skills/import-claude-context/SKILL.md`), and its main task is expected to take seconds — but on 2026-09-11 (desktop v0.6.8-rc3, engine 4b02fbaf) the core answered the greeting, ran its boot recap and went idle without starting it, so at 322 s the prose rule above would have archived a task the owner had just consented to and posted it in the recovery DM instead of letting the watcher's startup sweep re-emit it. Its marker is `<workspace>/data/claude-import/status.json` — written by `index.py` in the same seconds as the acknowledgement DM (`results/proactive-<ts>.txt`, which the bridge claims and archives, so it cannot serve as a marker). **It is read as *this task's* only by identity, never by timestamp:** `status.json` is one global file, and every phase the import scripts write carries the `task_id` the run was started with (`index.py --task-id <id>` — `skills/import-claude-context/SKILL.md` step 1 — stores `run: {task_id, run_id}` in `state.json`, and `_common.write_status` stamps both onto every status). A status is **bound** to a task when `status.task_id == <id>`. A status bound to a *different* task is, for this task, the same as no status (#4177 review: an older run A ending after a genuine new request B was queued satisfied "newer than the task" and B was archived as done without ever executing — the `absent` row classified `orphan` correctly, the timestamp was never a receipt). A status with **no** `task_id` (a pre-#4177 writer, or `index.py` run without `--task-id`) that post-dates the task may be this task's run or another's: not enough to archive, enough to report — fail toward recovery, since a spurious DM line costs a line and a wrong archive costs the owner's import.
   - bound and at a **terminal phase** — `done`, `staged`, `discarded`, `forgot` — → **DONE** (the run reached its end; archive). `write_status` produces exactly eight phases across `skills/import-claude-context/scripts/`: `indexed` (index.py), `extracted` (extract.py), `summarizing` / `rolling-up` (progress.py), `staged` (progress.py, finalize.py stage and partial commit), `done` (finalize.py commit), `discarded`, `forgot`. `staged` is terminal because the digest has been posted and the next step is an owner reply, which arrives as a *new* task; `discarded` / `forgot` are the owner ending the run. The classifier's test pins this set against the scripts, so a new writer phase fails the suite until it is placed on one side; an unknown phase at runtime is treated as resumable.
   - bound, resumable phase, and `status.json` last moved **≥ 3600 s (1 h)** ago (`IMPORT_STALL_S`; precedent `schedule-crons` `active_stale_minutes`) → **IMPORT-STALLED**: leave `tasks/<id>.txt` alone (the watcher's sweep still re-emits it and the run resumes from disk — a machine that slept mid-run self-heals), but **list it in step 3b's DM** so the owner learns about a run that can never advance (corrupt source, a phase that never moves). Log: `import-stalled: consented import stalled at phase <p>, status.json last moved <idle>s ago`.
   - bound, resumable phase, moved within the hour → **IMPORT-RESUME**: leave `tasks/<id>.txt` alone whatever its age, never archive it, never list it in step 3b's DM. The watcher's sweep re-emits it and the skill resumes from disk. Log: `import-resume: consented import already started (phase <p>)`.
   - **unbound** — `status.json` newer than the task but with no `task_id` → **IMPORT-UNBOUND**: leave `tasks/<id>.txt` alone (never archive it; the sweep re-emits it and the idempotent skill re-runs or resumes), but **list it in step 3b's DM** with the re-run line. Log: `import-unbound: an import run started after this task was queued (phase <p>) but status.json carries no task_id`.
   - not started — no `status.json`, one bound to another task, or an unbound one older than the task — and younger than **1800 s (30 min)** → FRESH.
   - not started and older → ORPHAN like any other task (step 3 aggregates it; the re-queue line in the DM restarts it, and so does the owner saying "import my Claude history"). This is the verdict for request B in the interleaving above: it was never executed, which is exactly what the owner must hear.

   **The "never orphaned" guarantee is bounded, and here is the window.** `index.py` writes `status.json` (`indexed`) *after* the index files and state (`index.py` line ~482–490), so the durable "started" flag lands after step 1's effect, not before it. An import that dies mid-index — between the acknowledgement and that write — leaves no marker, gets the 30-minute line above, and is then orphaned like any other task: archived, listed in the DM with its re-queue line, restartable by the owner saying "import my Claude history". That outcome is recoverable, so the write is not reordered; but a started import is only never-orphaned from the `indexed` write onward.

4. **Classify outcome**:
   - **DONE** → archive the task file: `mv tasks/<id>.txt tasks/archive/<id>.txt`. Log: `done: completion marker found at <path>`.
   - **DELIVERED** → leave alone. Log: `delivered: held by worker <inbox>, its finish archives it`.
   - **FRESH** → leave alone. Log: `fresh: arrived <N>s ago, watcher will handle`.
   - **IMPORT-RESUME** → leave alone (see step 3a). Log: `import-resume: consented import already started (phase <p>)`.
   - **IMPORT-STALLED** → leave alone, but append it to the in-pass `stalled_imports` list so step 3b names it in the DM (see step 3a). Log: `import-stalled: consented import stalled at phase <p>, status.json last moved <idle>s ago`.
   - **IMPORT-UNBOUND** → leave alone, but append it to the in-pass `unbound_imports` list so step 3b names it in the DM (see step 3a). Log: `import-unbound: import run at phase <p> cannot be matched to this request (status.json has no task_id)`.
   - **ORPHAN** → write a recovery result: see step 3.

### Step 3 — Recover orphan tasks (tier-aware)

ORPHAN handling depends on `source` because text-side recovery only makes sense for surfaces where the owner can still see and act on the DM. Voice/phone conversations don't replay in text. The flat per-task sentinel that v0.1.1 used was the wrong default for high-volume team-tier orphans whose conversation threads had moved on — see `feedback_orphan_check_tier_classify_before_sentinel` for the 2026-05-26 post-mortem (22-message blast across 5 channels, 13 of them into one active episode thread).

**Invocation contract.** orphan-check runs once at `/startup` step 1 (PR #1072), BEFORE the task watcher attaches. Under that contract the deferred branch (row 2 of the table below) has no race window — the watcher isn't running between step 3's defer-decision and step 3b's archive mv. If you invoke `/task-orphan-check` standalone with a live watcher already attached, the deferred branch is racy: the watcher may pick up the un-archived `tasks/<id>.txt` between step 3 and step 3b and re-classify it as FRESH or ORPHAN on its own pass (no `results/<id>.txt` exists yet, so the completion-marker check in step 2.2 won't catch it). Safe workaround for standalone invocation: rename `tasks/<id>.txt` → `tasks/<id>.txt.deferred` at defer-time so the watcher's `task-*.txt` glob skips it, then have step 3b mv `tasks/<id>.txt.deferred` → `tasks/archive/<id>.txt` atomically. Verify the watcher's glob actually excludes the suffix on your installation before relying on it.

**Decision table — apply per orphan, first match wins:**

| `source` | Action |
|----------|--------|
| `voice` / `phone` (any tier) | **Silent archive.** Text recovery to a voice/phone surface is the wrong shape; the conversation has hung up or moved on. `mv tasks/<id>.txt tasks/archive/<id>.txt`. No result write. Log: `archived-silent: voice/phone source`. |
| any other source (incl. `discord` / `telegram` / `slack` / `chat` / `whatsapp` / `email` / missing field / future surfaces) | **Defer; aggregate in step 3b.** Append `<id>` to an in-pass `deferred_orphans` list (along with its `access_tier` for per-tier counting). Do NOT write a per-task result. Do NOT archive yet — leave the task file in `tasks/` so step 3b consumes it (see "Invocation contract" above for the standalone-invocation `.deferred` suffix workaround). Log: `deferred: queued for consolidated DM (source=<source>, tier=<tier>)`. |

**All tiers (owner / team / other) flow through the same aggregated DM** so the owner has visibility into stale tasks across all tiers — previously, team/other-tier orphans were `[no-send]`-archived and the owner had no record. Preview-extraction in step 3b strips the bridge-injected system-instructions block so non-owner previews show the actual user ask, not boilerplate.

#### Step 3b — Aggregate deferred orphans into ONE proactive DM

Run once at the end of the orphan pass, after every orphan has been classified by the table above. If `deferred_orphans`, `stalled_imports` **and** `unbound_imports` are all empty, skip. (A stalled or unbound import alone still earns the DM — it is the only surface that run reaches.)

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

3. Resolve a **readable channel label** for each orphan, then group.

   `channel_id` alone is unreadable in a report — `!JzcRmAhNYbiWhIWNCL:ag2.space`
   tells the owner nothing about which conversation stalled. The task file already
   carries the name the bridge saw:

   ```python
   name = header.get("room_name") or header.get("channel_name")   # bridges write one of these
   cid  = header.get("channel_id") or header.get("chat_id") or ""
   label = f"{name} ({cid})" if name else (cid or "DM")
   ```

   Use `label` everywhere the report shows a channel. Keep the id: it is what
   `contextNotFrom` and re-queue commands key on, so dropping it trades one
   unreadable report for an unactionable one. Group `deferred_orphans` by
   `access_tier` (owner / team / other) → per-tier counts; and by `label` →
   per-channel counts.

4. Apply step 3c bomb-guard (see below) to decide whether to truncate the preview list.

5. Write `<workspace>/results/proactive-orphan-recovery-${ts}.txt`:

   ```
   Orphan recovery — N stale tasks from a prior session (oldest <Nm>, newest <Nm>, no completion markers).

   By tier: owner (<o>), team (<t>), other (<r>).
   By channel: <name> (<id>) — <x>; <name2> (<id2>) — <y>; DM — <z>; ...

   Previews (most-recent first, first ~100 chars of task body; in-band system instructions stripped):
   - task-<id> [<tier>, <channel label>, <Nm ago>]: <preview>
   - ...
   [If truncated by step 3c: "+<N-20> more — see tasks/archive/ for the full list."]

   [If `stalled_imports` is non-empty, one line per entry, from the classifier's `import_phase` / `import_idle_s`:]
   Import stalled at phase <p> since <idle, e.g. 3d 2h> (task-<id>, still in tasks/ — it resumes on the next sweep; say "import my Claude history" to resume it now, or `/import-claude-context --discard` to drop the run).

   [If `unbound_imports` is non-empty, one line per entry, from the classifier's `import_phase` / `import_idle_s`:]
   An import run started (phase <p>, last moved <idle> ago) but cannot be matched to this request (task-<id>, still in tasks/ — its status carries no task id); say "import my Claude history" to re-run it, or `/import-claude-context --discard` to drop the run.

   To re-queue an individual task: `mv "$(bash scripts/sutando-config.sh workspace)/tasks/archive/task-<id>.txt" "$(bash scripts/sutando-config.sh workspace)/tasks/"` (M0 helper resolves to `<workspace>/tasks/...` — `<repo>/workspace/tasks/...` by default).
   The archived file retains its original body (incl. system-instructions block for non-owner tasks), so re-queueing preserves sandboxing.
   If none still matter: no action needed — they're already archived.
   ```

6. For each `<id>` in `deferred_orphans`: `mv tasks/<id>.txt tasks/archive/<id>.txt`. Entries of `stalled_imports` and `unbound_imports` are **not** moved — they stay in `tasks/` so the watcher's sweep can resume or re-run them.

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
  left for the watcher as a started import (import-resume): I
  left for the watcher but reported as stalled (import-stalled): S
  left with a pool worker that accepted it (delivered): D
  left for the watcher but reported as unmatched (import-unbound): U
  recovered as orphan (sentinel result written): J
```

The summary lands in the conversation buffer so the agent's first turn (and operator) sees what happened. If `M+K+I+S+U+J+D ≠ N`, the script bailed mid-pass — log a warning and let the operator investigate.

## What this DOES NOT touch

- `<workspace>/tasks/archive/` — graveyard; never modified except by this skill's own archive moves.
- The watcher (`watch-tasks-stream.sh`) — runs unchanged; just sees a smaller `tasks/` dir after orphan-check completes.
- The bridges (`discord-bridge.py`, `telegram-bridge.py`) — orphan-check reads their per-side-effect markers (`.sending` files from #1048) but never modifies them.
- `crons.json` or any scheduler state.
- Memory dir or `MEMORY.md`.

## Known residual risk: the <5min sub-window for non-Discord surfaces

A task that arrived <5 minutes before a crash, executed its side effect, then died before writing its result file has NO completion marker AND looks FRESH (age < 5min) → orphan-check leaves it for the watcher → side effect re-fires. Unavoidable without per-side-effect markers, and the `.sending` markers only close it for Discord.

**Currently covered:** Discord DM delivery (PR #1048's `.sending`), file presence in `results/`, the Claude-Code import's `data/claude-import/status.json` (step 3a — read by the `task_id` it carries, never by timestamp; the import is resumable, so this marker exempts rather than re-fires; it is written after step 1's index, so a run dying before that is orphaned recoverably at 30 min).

**Residual hole, in priority order:**
- Voice agent side effects (no marker file yet).
- Phone-call agent side effects (same).
- Telegram delivery (Telegram bridge doesn't yet ship a `.sending` analog of #1048).
- Generic API calls / shell mutations without their own marker file.

Conservative default for the hole: any orphan without a CLEAR completion marker gets the recovery-sentinel treatment, which surfaces to the operator rather than silently re-firing. As other bridges/tools grow their own per-side-effect markers, orphan-check should learn to read them at step 2 — the marker list is intentionally a code-level data table, not buried in prose.

## What it MIGHT need in the future

- **More side-effect markers** (see "Known residual risk" above): voice/phone/Telegram especially. Add them to `scripts/classify.py` (`completion_marker` / the import branch) and its test, then to the prose.
- **Promote the rest to a script.** Classification (step 2) became `scripts/classify.py` on 2026-09-11, when the rules grew past "marker-or-not + age-vs-5min" (the import exemption, step 3a). The recovery half (step 3's moves and the aggregated DM) is still agent-executed prose; mirror it too once a second special case appears.

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
- **#1066 (still open as of skill draft)** — VasiliyRad's bumper in-place-write fix. Becomes moot if #1049 is reverted. Recommend close as "superseded by /task-orphan-check."
- **#1072 (this PR's sibling)** — `/startup` skill. Invokes `/task-orphan-check` as step 1 if installed.

## Implementation note: classification is a script, recovery is prose

Step 2 runs `scripts/classify.py` (read-only, stdlib + `src/local_task_protocol.py`; `tests/task-orphan-check-classify.test.py`). It exists because the rules stopped being "marker-or-not + age-vs-5min" the day the import exemption (step 3a) was needed, and a rule that only lives in prose is applied by whichever reading the agent makes that boot — the 2026-09-11 run reasoned its way to "would now classify … as an ORPHAN" about a task it had itself left unstarted. Steps 3–5 (the archive moves, the aggregated DM, the summary) stay agent-executed: they are small per pass (typically 0-3 live tasks) and each is a plain Bash/Write call over the classifier's verdicts.

## Iteration log

- v0.1.0 — 2026-05-23 — initial draft. Per Chi 2026-05-23 Discord exchange about #1049 redesign ("simply ask the agent to check when starting"). Designed to be invoked from `/startup` step 1 (PR #1072). Standalone-callable for manual recovery. Replaces the attempts-counter approach (#1049 + #1066's followup) with a startup-time classification using existing side-effect markers (#1048's `.sending` files + result-file presence). No bumper, no in-band writes, no self-trigger loop.
- v0.1.1 — 2026-05-23 — qingyun-sutando review pass. **(1)** Fixed `<id>` ambiguity — `<id>` is the value of the `id:` header (already includes `task-` prefix); paths are `results/<id>.txt` NOT `results/task-<id>.txt` (the prior wording double-prefixed and would have misclassified every completed-but-unarchived task as ORPHAN → spurious recovery notes). **(2)** Age now derives from immutable header `timestamp:` / `task-<epoch-ms>` id, NOT file mtime (mtime resets on rsync / `git checkout` / `touch` / workspace sync, making old orphans look FRESH → re-fire). **(3)** Clarified `.sending` contract via new step 2b: `<id>.txt` (no suffix) = DONE, `<id>.txt.sending` = bridge mid-delivery (treat as DONE; bridge owns its own crash recovery via #1046/#1048's startup sweep). **(4)** Named the <5min residual hole explicitly under its own section, with prioritized coverage list (voice / phone / Telegram + generic API). **(5)** Noted "promote to scripts/orphan-check.py" trigger.
- v0.1.2 — 2026-05-26 — tier-aware orphan recovery. Step 2.1 now parses `access_tier:` (default `owner` for legacy task files lacking the field). Step 3 rewritten as a decision table branching on `source` + `access_tier`: voice/phone → silent archive; team/other → `[no-send]` archive; owner discord/telegram/slack/chat → defer to new step 3b (consolidated proactive DM aggregating all owner-tier orphans this pass into ONE `proactive-orphan-recovery-<ts>.txt` instead of N per-channel sentinels). New step 3c bomb-guard collapses any future >5-deliveries-to-one-channel into a single summary post (no-op today; defense for future branch additions). Triggered by 2026-05-26 noise-bomb post-mortem (`feedback_orphan_check_tier_classify_before_sentinel`): v0.1.1 sentinel-blasted 22 stale tasks across #ep013 (13), #talk (4), voice channels (4), and DM (1) — wrong default for high-volume team-tier orphans whose threads had moved on, and wrong shape (N per-channel posts) for owner-tier ones. Sibling work on cross-fleet bridges (qingyun-sutando MacBook branch) adds defensive bot-user_id tier-filter so peer bots' stale tasks don't tier as `owner` via allowFrom inheritance — that's the upstream cause of the same skill running on a sibling fleet seeing 21/22 of one fleet's orphans as `owner` rather than `team`.
- v0.1.3 — 2026-05-26 — liususan091219 (Maddy / MBP node) review pass on PR #1241. **(1)** Row 3's `source` column was an enumeration (`discord / telegram / slack / chat / unknown`), which under literal reading meant a task with `source: whatsapp` (or any future surface) + `access_tier: owner` matched no row and wedged in `tasks/` forever. Rewritten as a true catch-all (`any source not matched by row 1`). **(2)** Added explicit "Invocation contract" paragraph at the top of Step 3 documenting the assumed-no-race-window guarantee (orphan-check runs at `/startup` step 1 before the watcher attaches), plus a standalone-invocation workaround (`tasks/<id>.txt.deferred` suffix so the watcher's `task-*.txt` glob skips deferred-owner files between step 3 and step 3b). Sibling PR #1233 (bridge-side bot-sender tier-downgrade) closed at owner request 2026-05-27 01:44Z; this PR now standalone.
- v0.1.4 — 2026-05-27 — per Chi 17:50Z Discord. Decision table collapsed from 3 rows to 2 (voice/phone silent + everything-else aggregated). **Team/other-tier orphans now flow into the same proactive DM as owner-tier** (was: `[no-send]` archive, owner had zero visibility); aggregated DM gains a `By tier:` line so the tier-mix is scannable. Step 3b adds explicit preview-extraction that strips the bridge-injected `===SUTANDO SYSTEM INSTRUCTIONS===` block before slicing the first 100 chars — non-owner task bodies put the block at the FRONT, so unprocessed previews would have leaked boilerplate, not user content. Step 3c bomb-guard restructured: total-deferred-count cap (truncate preview list to 20 + "+X more" footer when >30 deferred) becomes layer 1; per-channel-delivery cap demoted to layer 2 (future-proofing no-op today). The system-instructions block is **preserved in the archived task file body** — only the preview-in-DM strips it. Re-queueing via `mv tasks/archive/<id>.txt tasks/` preserves sandboxing for non-owner tiers.
- v0.1.7 — 2026-09-11 — #4177 review, second pass (qingyun-wu, confirmed by john-the-dev). `status.json` is one global file with no task or run identity, so step 3a's "newer than the task" read was never a receipt: an older run A ending (`done` / `staged` / `discarded` / `forgot`) after a genuine new request B was queued made B `done`, and step 4 archived B without it ever executing — the `absent` row classified `orphan` correctly, and v0.1.6's terminal-phase expansion widened the hole from one status value to four. Now the writers carry identity (`index.py --task-id <id>` mints `run: {task_id, run_id, started_at}` into `state.json`; `_common.write_status` stamps both onto every status, so extract / progress / finalize carry them unchanged) and the classifier counts a status for a task only when `status.task_id == <id>`: bound → the v0.1.6 rules (done / import-resume / import-stalled); bound to another task → not started for this one (fresh under 30 min, else orphan with the re-queue line — B was never executed, which is what the owner must hear); no `task_id` and newer than the task → new verdict **IMPORT-UNBOUND**, never archived, listed in the DM with the re-run line — fail toward recovery. The row carries `import_task_id`. Pinned: the interleaving for every terminal phase (B `orphan` past the line / `fresh` under it, never `done`), the matching-run control (same status with B's id → `done`), the legacy status kept as `import-unbound`, resume / stalled requiring the bound id, and the importer's real `write_status` end to end.
- v0.1.6 — 2026-09-11 — #4177 review (qingyun-wu, john-the-dev). **(1)** The import-task match was a bare `import-claude-context` substring over any owner body, so an unrelated three-day-old owner task quoting the skill's path ("Review PR 4177 which touches skills/import-claude-context/SKILL.md") classified `import-resume` and was parked in `tasks/` forever, absent from the recovery DM — a regression against main's orphan + re-queue line. Now the match is a run *intent*: the wizard header, `/import-claude-context` as a standalone token (not inside a path), or a documented trigger sentence, case-insensitive; the reviewer's case is a test, with a control that adds each real trigger to the same wording. **(2)** `import-resume` had treated every phase but `done` as resumable, but `write_status` also produces `staged`, `discarded`, `forgot` — an import the owner explicitly discarded parked its task file indefinitely. The eight writer phases are enumerated and pinned against the scripts; `done` / `staged` / `discarded` / `forgot` are terminal (→ DONE), `indexed` / `extracted` / `summarizing` / `rolling-up` and anything unknown are resumable. **(3)** `started` had no recency bound: an import that died mid-run (machine slept) read `import-resume` at any age and reached no surface. New `import-stalled` verdict when `status.json` has not moved for 3600 s — still left in `tasks/` so the sweep resumes it, but listed in the DM. **(4)** Named the bounded window of the never-orphan claim: the `indexed` marker lands after step 1's index write, so a run dying before it orphans (recoverably) at 30 min.
- v0.1.5 — 2026-09-11 — step 2 promoted to `scripts/classify.py` (+ `tests/task-orphan-check-classify.test.py`); new step 3a. Trigger: desktop v0.6.8-rc3 (engine 4b02fbaf) queued the consented Claude-Code import as `task-claude-import-<ms>.txt` (`channel_id: onboarding-wizard`, task-mid, `priority: low`); the core answered the owner's greeting, finished the startup ceremony (boot recap, `/startup complete`) and went idle without starting it, then reasoned that "the orphan-check rule would now classify task-claude-import-… as an ORPHAN" at 322 s. Under the 5-minute line that consented, resumable task would have been archived into the recovery DM on the next boot instead of being re-emitted by the watcher's sweep. Now: an import task whose run started (`data/claude-import/status.json` newer than the task) is `import-resume` — left in `tasks/`, never archived, whatever its age; one that has not started gets a 30-minute line; phase `done` after the task is a completion marker. Header reads use the lenient (shape-union) parser because the desktop writer is task-mid. The desktop is moving the import request into the first-contact DM message, so the task-file form is a legacy/fallback trigger — kept covered because installed clients still write it.
