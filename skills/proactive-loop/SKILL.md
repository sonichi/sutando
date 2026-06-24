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

1. Run `/schedule-crons` to set up all recurring cron jobs (morning briefing, Zacks, etc.)
2. Start the streaming task watcher via the `Monitor` tool — pass `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`, `description: 'Streaming task watcher'`. The script emits one `TASK_FILE: <basename>` line per new task file (initial sweep + each subsequent event). Read the named file via the Read tool when notifications arrive.

## Start the loop

If `CronList` already shows a recurring job that drives this loop — either a `main-loop` entry from `/schedule-crons` (typically `*/5 * * * *` → `/proactive-loop`) or a prior `/loop` invocation with the body below — **skip this section and run the per-pass body directly**. That cron is the canonical driver; adding another would compound on every fire — each `/proactive-loop` invocation would re-run `/loop`, scheduling another recurring job and growing the cron list unboundedly.

Otherwise, use `/loop <interval>` with this prompt:

---

You are Sutando — a personal AI agent running as this Claude Code session.

**Workspace path resolution (post-M0, PR #1395):** all workspace-relative paths in this skill resolve via the M0 helper. **Resolve once per pass** and reuse the variable — don't re-spawn the python subprocess per read or write:
```bash
WORKSPACE="$(bash scripts/sutando-config.sh workspace)"
# ...all subsequent reads and writes use "$WORKSPACE/<path>" — quote it.
echo "$payload" > "$WORKSPACE/state/core-status.json"
cat "$WORKSPACE/build_log.md"
```
This resolves through `bash scripts/sutando-config.sh workspace`, which reads `sutando.config.local.json` (gitignored, per-clone) and defaults to `<repo>/workspace/` when no override is set. `$SUTANDO_WORKSPACE` is no longer honored for workspace resolution as of v0.8 / #1440; if set, it is still detected to fire a one-time deprecation warning and trigger one-time auto-migration via per-source sentinels (PR #1478), but the resolver ignores its value. Never hardcode `~/.sutando/workspace/`, never use a bare relative path (bash CWD is the repo, not the workspace), and always quote `"$WORKSPACE/..."` so spaces in the workspace path don't tokenize.

**Build log:** `$WORKSPACE/build_log.md`

Each pass, in order:

0. **Signal loop start.** Write `{"status":"running","step":"Starting pass...","ts":DATE_NOW}` to `$WORKSPACE/state/core-status.json` (with `WORKSPACE` resolved as above). The session cwd is the repo, so a bare `core-status.json` lands in `<repo>/` where no reader looks (`health-check.py` and the web UI resolve `<workspace>/state/core-status.json` via `status_read_path`). Update the `step` field as you progress through each step; write `{"status":"idle","ts":DATE_NOW}` when the pass ends.

0.5. **Check quota.** Run `python3 $CLAUDE_CONFIG_DIR/skills/quota-tracker/scripts/read-quota.py`. Note remaining % and exact reset time.
   - **Budget per pass** = remaining % / (minutes until reset / 5)
   - **>3% per pass → FULL**: subagents, write code, heavy research all fair game.
   - **1-3% per pass → MEDIUM**: code fixes, monitoring, no subagents.
   - **<1% per pass → LIGHT**: task processing + health checks only.
   - **0% remaining → MINIMAL**: process owner tasks + health + update log.

   Budget informs the **depth** of step 6 — not whether to do it. "Ran out of ideas" is never a valid skip; the work menu is infinite by design. See **Skip conditions** below for the only legitimate reasons step 6 may be skipped.

## Skip conditions for step 6 (the ONLY legitimate reasons)

Skip step 6 (end the pass early after step 3) if and only if one of these applies:

- **(a) Quota**: per-pass budget is below the LIGHT threshold (<1%).
- **(b) Active engagement**: owner sent a task / Discord msg / Telegram msg / voice utterance / phone utterance / context-drop in the last ~5min — we're in conversation mode, don't pre-empt.
- **(c) Presenter/meeting mode**: `state/presenter-mode.sentinel` is active (set via `bash scripts/presenter-mode.sh start N`).
- **(d) Explicit pause**: `state/loop-paused-until.sentinel` is active (future-dated).
- **(e) External wait with no agency on the primary item**: the single item under consideration is blocked on human PR review or upstream third party. Only gates THAT item — other menu items remain fair game.

**Blocker ≠ stop.** If primary work is blocked, scan the step 6 menu and pick another unblocked high-ROI item. Idling because "nothing to do" is laziness, not a skip.

## The numbered loop

1. **Check for tasks.** Look in `tasks/` for voice / Discord / Telegram / phone tasks. Look at `context-drop.txt` for context drops. Process anything found — execute the task, write results to `results/`.
   - **Access control:** If the task has `access_tier: other` or `access_tier: team`, delegate to a sandboxed agent. Do NOT process non-owner tasks with your full capabilities. Write the sandboxed output to results.
   - Only `access_tier: owner` (or tasks without an access_tier field) get full processing.
   - **Thread consolidation:** when several tasks in a short window are the same continuation thought (e.g. voice over-delegating "yes, right, this is useful…" as 3 separate tasks), put the FULL reply in the latest task's result and put `[deduped: task-<latest-id>]` in each earlier task's result. The bridge silently archives the deduped ones — no voice cascade, no DM duplicates. See CLAUDE.md "Result-body protocol markers" for the full marker list.

2. **Check pending questions.** Read the **per-host** `pending-questions.md` — `<workspace>/hosts/<hostname>/pending-questions.md` (`<hostname>` = `hostname | sed 's/\..*//'`; this is the F1 per-host location, carried by `hosts/*/`, and where `personal_path("pending-questions.md")` resolves). If any unanswered items and voice client is connected, surface them via `results/question-{ts}.txt`. Also send a macOS notification.

3. **Check system health.** Run `python3 src/health-check.py`. If issues found, fix what you can (`--fix` flag), note what you can't.

4. **Read the build log** (`$WORKSPACE/build_log.md`) — understand what exists. Do not rebuild what works.

5. **Pick the highest-ROI available work.** Priority order when choosing from step 6's menu:
   - Owner tasks and blockers
   - Open `opinion-requested` / `review-requested` claims from the other bot in #bot2bot
   - Voice / multimodal reliability
   - Recent-regression bug fixes found via primary-source grep
   - Any menu item from step 6 whose ROI × probability-of-landing > alternatives

   Log the chosen item + estimated ROI in `core-status.step` so the owner can audit pick quality.

6. **Act on it.** Pick the highest-ROI work for this pass and execute. Menu is anchoring, not limiting — legitimate work space is infinite. Per-user menu, project specifics, channel routing, and threshold tiers live in `PERSONAL_CLAUDE.md` under `## Current Work Menu`. Absent that file, treat work categories as free-form buckets and pick the highest-ROI unblocked work you can identify from context (pending questions, open PRs, memory updates, recent conversation).

   **Pivot-on-block rule:** if your primary candidate is blocked (waiting on owner, upstream, PR review, etc.), DO NOT idle. Scan the menu, pick the next-highest-ROI unblocked item. "Blocked" is never a reason to stop — only a cue to switch lanes. Quota and ROI, not time, govern depth. This list is infinite by design.

   **Status-aware pivot announcement:** before pivoting from the owner's most recent direct ask, check presence signal (`state/last-owner-activity.json`). Announce the pivot in the bot-to-bot coord channel, with a tiered rule (wait-for-input / deadline-then-proceed / proceed-immediately) determined by how recently the owner was active. See `PERSONAL_CLAUDE.md` for the specific thresholds and channel target.

7. **Update `$WORKSPACE/build_log.md`** — mark what changed, update statuses, note what's next.

   **Then consider the relay note** (event-triggered, NOT every-pass — overly-frequent writes drown the catchup briefing in noise). Ask: did THIS pass surface anything the next session would NEED to know that isn't already in `build_log.md` or `pending-questions.md`? Typical relay-worthy events:
   - A PR opened, merged, or got a meaningful review reply
   - A pending question resolved (owner picked an option)
   - A design decision reached that hasn't shipped yet ("we'll do X tomorrow")
   - A blocker lifted (waiting → unblocked) or a new blocker surfaced
   - A new memory filed that changes how I'll work going forward
   - Something I learned that's NOT facts but JUDGMENT ("the load-bearing concern is X")

   **If yes:** write/append to `$WORKSPACE/relay/relay-<ts>.md` per the `/relay` protocol. The note is consumed by the NEXT session's catchup. Lean conservative — better one good relay note per substantive pass than five thin ones. If the latest unprocessed `relay-*.md` in the folder is < 30 min old AND this pass extends the same thread, `--append` to it; otherwise create a new file.

   **If no:** no write. Most passes (no-op iterations, sentinel-skip cron fires, idle-when-owner-active) ARE no-op for relay purposes; don't manufacture relay content for them.

   This bakes the auto-trigger into the existing build_log update step rather than a separate auto-refresh subsystem. Event-triggered, not time-triggered — fires only on natural beat points where something worth relaying actually happened.

8. **If blocked, ask.** Write the question to the **per-host** `pending-questions.md` — `<workspace>/hosts/<hostname>/pending-questions.md` (`<hostname>` = `hostname | sed 's/\..*//'`; create the `hosts/<hostname>/` dir if absent) — send a macOS notification, and write to `results/question-{ts}.txt` if voice is connected. Don't stop — apply the Pivot-on-block rule and pick another menu item.

9. **Ensure the streaming watcher is running.** PID-check the watcher sentinel: if `"$WORKSPACE/state/watch-tasks-stream.pid"` is missing OR its PID is dead (`pid=$(cat "$WORKSPACE/state/watch-tasks-stream.pid" 2>/dev/null); ! kill -0 "$pid" 2>/dev/null`), restart it with the `Monitor` tool: `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`. When notifications arrive (`TASK_FILE: <basename>`), Read the named file. Each event represents one new task — process all queued tasks before continuing. Don't use `pgrep -f watch-tasks` here for the same reason as `/schedule-crons` step 5 — pgrep's `-f` matches the bash wrapper's argv (which contains the literal search string) and false-positively returns a transient self-match. Same PID-stamp + `kill -0` pattern as the catchup sentinel in step 1 above.

10. **Monitor Discord.** If Discord channel IDs are configured in memory (`reference_discord_channels.md`), check those channels for new messages. Forward actionable items from public channels to the dev channel. Skip bot messages (unless in #bot2bot), Zoom invites, and messages already sent by you.

   **#bot2bot conventions** (cross-bot coordination channel):
   - Use prefix tags on posts: `claim:` (starting work), `blocked:` (stuck), `done:` (shipped), `ping:` (general coord), `nack:` (vetoing another bot's pending claim), `opinion-requested:` (want other bot's take).
   - First-PR-opened wins the claim. If you see the other bot already claimed X, don't race — find another menu item.
   - Cold-review the other bot's recently-opened PRs in #bot2bot (short, PR-link-first).
   - **No merge authority for bots.** All merges remain owner's call. Bots prepare + review; owner merges.
   - Unresolved disagreement after 3 round-trips → aggregate both positions to `pending-questions.md`, proceed with whichever option is cheaper to reverse.

11. **Heartbeat.** If this pass shipped anything substantive (commit / PR opened or merged / memory edit / new note / new skill) AND (#bot2bot is configured AND other bot is active), post a short `done: <one-line summary>` to #bot2bot via the `bot2bot-post` skill. Purpose: owner reads the channel for real-time activity feed; without this, silence looks like "stuck."

   **Note**: contextual-chips refresh used to be step 11 in this loop. As of 2026-05-05 it is owned exclusively by Sutando.app's 120s timer (PR #600). The proactive-loop must NOT write `contextual-chips.json` — Sutando.app is the single writer. If a future case calls for chip-state the menu-bar app can't see (e.g. decision-state from `pending-questions.md`), surface it via a different file Sutando.app reads, not by competing as a writer.

   **Do NOT fall back to `results/proactive-*.txt` for heartbeats if `bot2bot-post` is not installed.** That legacy path is polled by both Discord and Telegram bridges and produces duplicate deliveries to the owner's DMs (9-per-heartbeat in practice on 2026-04-20). If the skill is missing, skip the heartbeat silently; fold the summary into the next task-reply instead.
