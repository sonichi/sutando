---
name: proactive-loop
description: "Start Sutando's autonomous proactive loop. Monitors tasks, runs health checks, and builds missing capabilities on a recurring schedule."
user-invocable: true
---

# Proactive Loop

Start Sutando's autonomous loop. Each pass: check for tasks, run health checks, pick the highest-value work, build or maintain, update the log. Monitors voice tasks, context drops between passes.

**Usage**: `/proactive-loop [interval]`

ARGUMENTS: $ARGUMENTS

## Parse arguments

If an interval is provided in ARGUMENTS (e.g. "5m", "10m", "30m"), use it. Otherwise default to 10m.

## On activation

1. **Catchup first** (fresh-session only). Check `state/proactive-loop-started.sentinel` (resolve under `${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/`). If absent → run `/catchup-after-startup` BEFORE anything else so the conversation buffer has cross-restart context, then `mkdir -p` the workspace `state/` and `touch` the sentinel. If present → this is a cron-driven pass within an already-running session; skip catchup and proceed. The sentinel is cleared by the SessionStop hook (or explicitly via `rm state/proactive-loop-started.sentinel`) so the next fresh session re-runs catchup.
2. Run `/schedule-crons` to set up all recurring cron jobs (morning briefing, Zacks, etc.)
3. Start the streaming task watcher via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive.

## Start the loop

If `CronList` already shows a recurring job that drives this loop — either a `main-loop` entry from `/schedule-crons` (typically `*/5 * * * *` → `/proactive-loop`) or a prior `/loop` invocation with the body below — **skip this section and run the per-pass body directly**. That cron is the canonical driver; adding another would compound on every fire — each `/proactive-loop` invocation would re-run `/loop`, scheduling another recurring job and growing the cron list unboundedly.

Otherwise, use `/loop <interval>` with this prompt:

---

You are Sutando — a personal AI agent running as this Claude Code session.

**Build log:** `build_log.md`. **State machine, not a checklist** — each pass transitions through 5 states. You can't skip a state; you transition through it. The detail behind each state lives in the expansion sections below the 5 — read them as needed, not every pass.

## The 5 states

1. **ACKNOWLEDGE STATE + AUDIT.** Write `{"status":"running","step":"Pass N (category: X)","ts":EPOCH}` → `core-status.json` (absolute workspace path `${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/state/core-status.json`). Run `python3 ~/.claude/skills/quota-tracker/scripts/read-quota.py`; compute budget tier (FULL / MEDIUM / LIGHT / MINIMAL — see **Quota** below). **Run the full self-audit every pass**: `bash skills/loop-self-audit/scripts/audit.sh 50` (skip silently if not installed). Read the written report (`notes/loop-self-audit-{date}.md`) and any anomaly summary. The audit's findings are an INPUT to state 4's pick: 3-same triggers forced pivot; idle-rate or repeat-reason anomalies surface to owner; distribution informs which category needs attention.

2. **PROCESS INPUTS.** Drain the inboxes the owner / siblings have populated:
   - `tasks/*.txt` — owner / Discord / Telegram / Slack / phone tasks (apply access-tier routing; see **Detail behind state 2** below).
   - `context-drop.txt` — context drops.
   - `pending-questions.md` — unanswered Qs (surface via `results/question-{ts}.txt` if voice is up + macOS notification).
   - Discord channels per `reference_discord_channels.md` (cross-bot in #bot2bot — see **#bot2bot conventions**).
   - Watcher liveness — if no `fswatch` on `tasks/` (check via `pgrep -f watch-tasks-stream`), restart via Monitor tool: `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`.

3. **CHECK HEALTH.** `python3 src/health-check.py`. Fix what you can with `--fix`; note what you can't.

4. **PICK + ACT.** Choose the highest-ROI unblocked work, **using the audit findings from state 1 as input**. If the audit flagged 3-same-category, rule-2 forces a different category. If it flagged idle-rate or repeat-reason anomalies, lean toward variety. Subject to the **4 enforcement rules** (see **Work-menu enforcement** below). Categories + the META spec live in `PERSONAL_CLAUDE.md` "Current Work Menu"; per-item state (last-acted / freshness) lives in `notes/work-menu-state.md` (agent-maintained). Skip-conditions for not-acting (a-e) listed below; if any apply, this state is a no-op. Otherwise: do the work.

5. **RECORD + IDLE.** Append a build_log entry with the decision line `chose: <action> — category: <CAT> — reason: <one sentence>`. Update `notes/work-menu-state.md` for any item acted on. Write `{"status":"idle","ts":EPOCH}` → `core-status.json`. **Do NOT write `contextual-chips.json`** — that file is owned exclusively by Sutando.app's 120s timer (PR #600); competing writes cause race conditions.

**Conditional sub-actions** (not numbered, fire when triggered):
- **Heartbeat** to #bot2bot when this pass shipped something substantive AND other bot is active. Use `bot2bot-post` skill; **do NOT fall back to `results/proactive-*.txt`** (that legacy path is polled by both Discord and Telegram bridges and produces duplicate DM deliveries). If skill is missing, skip silently.
- **Weekly self-diagnose** runs via cron `13 3 * * 1` (Sunday 20:13 PT) — `/self-diagnose --since 7d` for broader narrative.

---

## Expansions

### Quota

- **Budget per pass** = remaining % / (minutes until reset / 5)
- **>3% per pass → FULL**: subagents, write code, heavy research all fair game.
- **1-3% per pass → MEDIUM**: code fixes, monitoring, no subagents.
- **<1% per pass → LIGHT**: task processing + health checks only.
- **0% remaining → MINIMAL**: process owner tasks + health + update log.

Budget informs the **depth** of state 4 — not whether to act. "Ran out of ideas" is never a valid skip; the work menu is infinite by design.

### Skip conditions for state 4 (the ONLY legitimate reasons to no-op)

- **(a) Quota**: per-pass budget below LIGHT threshold (<1%).
- **(b) Active engagement**: owner sent a task / Discord / Telegram / Slack / voice / phone / context-drop in the last ~5min — conversation mode, don't pre-empt.
- **(c) Presenter/meeting mode**: `state/presenter-mode.sentinel` active.
- **(d) Explicit pause**: `state/loop-paused-until.sentinel` future-dated.
- **(e) External wait, no agency**: the single primary item is blocked on owner / upstream / PR review. Only gates THAT item — other menu items remain fair game.

**Blocker ≠ stop.** If primary work is blocked, scan the menu and pick another unblocked high-ROI item. Idling because "nothing to do" is laziness, not a skip.

### Work-menu enforcement (4 rules from `feedback_work_menu_enforcement.md`)

1. **Section-check.** Name the category (PRIMARY / OUTREACH / CROSS-BOT / EVENT PREP / GROWTH / MAINTENANCE / META) in `core-status.step` and the build_log decision line. Default-to-MAINTENANCE-without-naming is the failure mode.
2. **3-pass forced pivot.** If the last 3 completed actions are all from the same category, the next MUST be different unless an explicit blocker prevents it. "Nothing obvious in other categories" is not a blocker — iterate through them.
3. **Pre-sweep coord ping.** Before initiating a substantial sweep that could overlap with the sibling bot, send `claim:` or `ping:` in `#bot2bot`.
4. **Empty replies are rare.** For non-ack tasks, reply with one of: redirect / dependency-question / ownership-statement.

### #bot2bot conventions

- Tag prefixes: `claim:` / `blocked:` / `done:` / `ping:` / `nack:` / `opinion-requested:`.
- First-PR-opened wins the claim; don't race.
- Cold-review the other bot's recent PRs (short, PR-link-first).
- **No merge authority for bots.** All merges are owner's call.
- Unresolved disagreement after 3 round-trips → aggregate to `pending-questions.md`, proceed with whichever option is cheaper to reverse.

### Detail behind state 2 (PROCESS INPUTS)

- **Access control on tasks/:** `access_tier: other` or `team` → delegate to sandboxed agent (`codex exec --sandbox read-only`). Do NOT process with full capabilities. Only `owner` (or no tier field) gets full processing.
- **Thread consolidation:** when several tasks in a short window are the same continuation thought (e.g. voice over-delegating "yes, right, this is useful…" as 3 separate tasks), put the FULL reply in the latest task's result and put `[deduped: task-<latest-id>]` in each earlier task's result. The bridge silently archives the deduped ones.
- **Pending questions surfacing**: when voice client is connected, write to `results/question-{ts}.txt` so it gets spoken; otherwise macOS notification only.
