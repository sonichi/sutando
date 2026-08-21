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
bash scripts/core-status.sh running "<what you are actually doing>"
cat "$WORKSPACE/build_log.md"
```
This resolves through `bash scripts/sutando-config.sh workspace`, which reads `sutando.config.local.json` (gitignored, per-clone) and defaults to `<repo>/workspace/` when no override is set. `$SUTANDO_WORKSPACE` is no longer honored for workspace resolution as of v0.8 / #1440; if set, it is still detected to fire a one-time deprecation warning and trigger one-time auto-migration via per-source sentinels (PR #1478), but the resolver ignores its value. Never hardcode `~/.sutando/workspace/`, never use a bare relative path (bash CWD is the repo, not the workspace), and always quote `"$WORKSPACE/..."` so spaces in the workspace path don't tokenize.

**Build log:** `$WORKSPACE/build_log.md`

Each pass, in order:

0. **Signal loop start.** Run `bash scripts/core-status.sh running "<short description of what you are actually doing>"`. **Do not `>` a JSON literal at the file** — the redirect truncates before it writes, and a reader polling in that window sees a zero-length file (`busy()` read that as idle and authorised a kill, #3156). The wrapper writes atomically and stamps `ts` for you. The session cwd is the repo, so a bare `core-status.json` lands in `<repo>/` where no reader looks (`health-check.py` and the web UI resolve `<workspace>/state/core-status.json` via `status_read_path`). Re-run it with a new description as you progress — a stale `step` actively lies to him. Run `bash scripts/core-status.sh idle` when the pass ends.

   **`step` is an owner-facing live message, not internal telemetry.** With `SUTANDO_PROGRESS_STREAM=1` (ON in the running bridge) the Discord bridge renders it to the owner verbatim as `⏳ <step> (Ns)` while he waits on an owner task, via `progress_stream.format_progress`. A generic placeholder ("Starting pass...", "running") shows up in his DM as noise; when processing an owner task, `step` should say what he is waiting on. Rewrite it on every pivot — a stale `step` actively lies to him. See memory `feedback_rich_core_status_step`. (This template previously read `"Starting pass..."` — the exact string that memory names as the anti-pattern, which is why the mistake kept recurring across compactions: this file is loaded every pass, the memory only when recalled.)

0.5. **Check quota (runtime-conditional — pick the branch for the core you are).**

   **Claude core** — run `python3 $CLAUDE_CONFIG_DIR/skills/quota-tracker/scripts/read-quota.py`. Note remaining % and exact reset time.
   **Tier EACH window by its OWN rule, then take the MOST RESTRICTIVE TIER.** `read-quota.py`
   reports two windows and they are scored differently — do not apply one window's thresholds to the
   other, and do not pick a window by largest `burn`. Those select differently: a short window can show a huge `burn`
   from one early burst while still holding more headroom than the window that actually limits you.
   `5h` at 19% used / 2% elapsed gives `burn 9.50, headroom 0.827` (MEDIUM); `7d` at 90% used / 70%
   elapsed gives `burn 1.29, headroom 0.333` (LIGHT). Selecting on `burn` picks the 5h window and
   authorises MEDIUM work while the 7d pool — which cannot refill for days — is already LIGHT.
   `burn` explains *how you got here*; `headroom` is what constrains what you may still do.

   **Why 7d needs its own rule:** the absolute per-pass thresholds are a *constant* on it. Every
   reachable 7d input yields LIGHT — 100% remaining over a full week gives 0.0496%/pass, 1% remaining
   gives 0.0005%/pass, and even 1% remaining with one hour to reset gives 0.083%/pass. Reaching
   MEDIUM would require 2016% remaining. A rule whose best case and worst case agree is not
   measuring anything. So 7d is paced against its own even pace, as a ratio:

   ```
   elapsed  = (now - window_start) / (window_reset - window_start)
   burn     = used% / elapsed           # >1 means ahead of even pace
   headroom = remaining% / (1 - elapsed)  # <1 means the rest must be slower than even pace
   sustainable_vs_current = headroom / burn
   ```
   **7d window — tier by `headroom`:**
   - **headroom ≥ 1.5 → FULL**: subagents, write code, heavy research all fair game.
   - **headroom 0.8–1.5 → MEDIUM**: code fixes, monitoring, no subagents.
   - **headroom < 0.8 → LIGHT**: task processing + health checks only.

   **5h window — tier by its retained absolute budget**, `remaining % / (minutes to reset / 5)`:
   >3% FULL / 1–3% MEDIUM / <1% LIGHT. These were calibrated for this window; they are NOT
   interchangeable with the headroom bands and must not be applied to 7d (there they are a constant).

   **Then adopt the more restrictive of the two tiers** (FULL > MEDIUM > LIGHT), and name which
   window bound it. **0% remaining on either window → MINIMAL**: owner tasks + health + log only.

   Quote `sustainable_vs_current` when reporting pace, and **name the denominator** — "0.45x even
   pace" and "0.27x current pace" are the same state and differ by 1.67x.

   **Codex core** — run `python3 skills/proactive-loop/scripts/codex-quota-gate.py --json` (the Claude-only `quota-tracker` state is not a Codex signal). It reads the Codex CLI's weekly rate-limit snapshots and conservatively uses the least remaining percentage among recorded weekly limit lanes; missing or entirely stale telemetry fails closed to `LIGHT`.
   - **>20% remaining → FULL**: subagents, code, and heavier research are fair game.
   - **5–20% remaining → MEDIUM**: monitoring and bounded code fixes; no subagents.
   - **1–<5% remaining → LIGHT**: task processing, pending questions, and health checks only.
   - **0% remaining or unavailable/stale → LIGHT**: owner tasks, health, and the build-log update only.

   Either way: budget informs the **depth** of step 6 — not whether to do it when quota permits. When the branch resolves to `LIGHT`/`MINIMAL`, skip autonomous self-development/research in step 6 even if the self-development policy is enabled; owner-requested tasks, pending questions, health/service recovery, watcher maintenance, and the build-log update remain active. "Ran out of ideas" is never a valid skip; the work menu is infinite by design. See **Skip conditions** below for the other legitimate reasons step 6 may be skipped.

0.7. **Reconstruct context (every pass — don't recall, read).** Before interpreting the queue or acting on anything that depends on earlier context, **invoke the `context-reconstruct` skill** (an actual Skill-tool invocation — a "see X" reference does not load it). It reads `<workspace>/hosts/<hostname>/current-track.md` first (the pinned main-track goal + active sub-task + open decisions), then — as the situation needs — the live owner thread (`src/discord-read.py <channel_id> --serving <task channel_id>` (task-serving; gated) or `--operator` (autonomous pass)), per-host `pending-questions.md`, the latest `relay/relay-*.md`, and the `build_log.md` tail. Where the record differs from what you *think* is true, **trust the record**. Then **maintain** `<workspace>/hosts/<hostname>/current-track.md`: create it if absent, rewrite it when the track moves (owner redirected / thing shipped / decision resolved). This step is the load-bearing anti-erosion hook — over long/compacted sessions, felt confidence is confidently wrong; the fix is reading the durable record, not remembering it. (Restored 2026-07-13 after being dropped in the ~Jun 30 workspace-revamp SKILL.md rewrite; originally added 2026-06-25 — see the context-reconstruct skill's Practice log.)

## Skip conditions for step 6 (the ONLY legitimate reasons)

Skip step 6 (end the pass early after step 3) if and only if one of these applies:

- **(a) Quota**: the selected tier from step 0.5 is MINIMAL (i.e. the most-restrictive window has 0% remaining). A LIGHT tier does NOT skip step 6 — it caps its depth.
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

2. **Check pending questions.** Read the **per-host** `pending-questions.md` — `<workspace>/hosts/<hostname>/pending-questions.md` (`<hostname>` = `bash scripts/sutando-config.sh host-label`; this is the F1 per-host location, carried by `hosts/*/`, and where `personal_path("pending-questions.md")` resolves). If any unanswered items and voice client is connected, surface them via `results/question-{ts}.txt`. Also send a macOS notification.

3. **Check system health.** Run `python3 src/health-check.py`. If issues found, fix what you can (`--fix` flag), note what you can't.

   **⚠ A WARN IS A POINTER INTO THE RECORD, NOT NEW INFORMATION. Grep the PQ before investigating one.** Health-check warns are chronic by construction: they re-fire every pass until an owner decision lands, so the ones that survive are precisely the ones already investigated and parked. The warn text looks new every time and carries no memory of having been read — that mismatch is the whole trap.

   ```bash
   grep -in "<probe-name-or-keyword>" "$WORKSPACE/hosts/$(bash scripts/sutando-config.sh host-label)/pending-questions.md" | head
   ```

   One call, before any investigation costing more than a couple of tool calls. It either returns nothing or hands you your own prior write-up — with the measurements, the mechanism, and usually the proposed fix already in it. When it hits: **extend it with what is genuinely new, or say plainly that nothing is new.** Re-filing a weaker duplicate is the failure mode, and surfacing one to the owner as a discovery makes them read the same thing twice.

   This lives in the loop file rather than only in a memory because a memory loads when RECALLED while this file loads EVERY PASS. The rule already existed, stated sharply, and still failed repeatedly — placement was the defect, not precision.

3.5. **Apply the self-development policy gate.** Run:

   ```bash
   python3 skills/proactive-loop/scripts/self-development-enabled.py
   ```

   The command prints `enabled` or `disabled`. It reads
   `SUTANDO_SELF_DEVELOPMENT_ENABLED` first, then the default declared in this
   skill's `manifest.json`. The shipped default is enabled (`1`). Product
   deployments can set the environment variable to `0`.

   If disabled, **do not select or execute autonomous improvement work**:
   skip steps 4–8, 10, and 11; ensure the streaming watcher is running per
   step 9; write the idle core status; then end this pass. Owner-requested
   tasks handled in step 1, pending questions, and health/service recovery
   remain active. Disabling self-development does not turn Sutando off and
   does not prevent the owner from explicitly asking it to change code.
   Manual `/proactive-loop` invocation does not override the policy.

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

6.5. **Proactive-comm / idle-surface (do NOT skip — this is the anti-going-dark hook).** Restored 2026-07-13; originally built 2026-06-26 as a working-tree SKILL.md step (it ran — idle-streak.json proves it) that was never committed to the repo file and was lost in the ~Jun-30 workspace-revamp rewrite (same rewrite that dropped 0.7). Its absence is exactly why the owner kept flagging "proactive comm handling is still missing" — with no step here, the loop silently idle-closes to the terminal and the owner sees nothing.

   Classify this pass: **substantive** (processed a task, shipped a fix/PR, filed a memory, posted to owner) or **no-op** (nothing owner-visible happened). Maintain `state/idle-streak.json` `{streak, last_surfaced_hash, updated}`: substantive → `streak=0`; no-op → `streak++`.

   On the **first no-op** of a run (`streak >= 1`):
   1. **Generate, don't idle** — first widen the menu and actually try to produce a tangible artifact (peer-PR review, regression grep, parity verify, research, memory curation, own-PR CI). Gated ≠ nothing-to-do. Only if genuinely all-gated go to step 2.
   2. **Surface once per changed set** — build the held-list (each item + who it's gated on), `sha1` it. If `hash != last_surfaced_hash`: post ONE concise "here's what's held / needs you (FYI, not a block)" line to the **owner's primary channel** (see `PERSONAL_CLAUDE.md` channel routing — NOT the `#bot2bot` coord channel), then set `last_surfaced_hash`. If `hash == last_surfaced_hash`: stay quiet **only if the owner is away/asleep** (`last-owner-activity.json` older than ~30 min); if he's been active in the last ~30 min, never go dark — drop a one-line progress/activity signal to his channel anyway.

   **Guardrails (all owner-corrected):** the surface is a non-blocking FYI footnote — NEVER a new wait-state ("awaiting your go" is not a reason to pause; keep doing the next unblocked thing). Don't spam: one signal per changed set / per work-shift, not per file. Presence is the discriminator: recently-active → never silent; genuinely-away → dedup-quiet is fine.

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

8. **If blocked, ask.** Write the question to the **per-host** `pending-questions.md` — `<workspace>/hosts/<hostname>/pending-questions.md` (`<hostname>` = `bash scripts/sutando-config.sh host-label`; create the `hosts/<hostname>/` dir if absent) — send a macOS notification, and write to `results/question-{ts}.txt` if voice is connected. Don't stop — apply the Pivot-on-block rule and pick another menu item.

   **⚠ INSERT ABOVE THE `# Resolved` DIVIDER, NEVER `>>` AT EOF (2026-08-02, twice in one session).** Every reader — `check-pending-questions.py`, morning-briefing, agent-api, friction-detector, dashboard — counts only the text ABOVE the file's top-level `# Resolved` line; everything below it is the audit trail. `cat >> "$PQ"` appends at EOF, which on this host is **500 lines below the divider**, so the question lands in the archive and is never counted.

   **⚠⚠ AND PLACE IT BY IMPORTANCE, AT THE TOP — "above the divider" is NOT enough (2026-08-20).**
   The instruction above is correct and load-bearing, but for an append-style writer "above the
   divider" means the **last position of the active region** — so the documented cure for
   archive-invisibility prescribes the exact position that causes **prefix-invisibility**. The
   notifiers render fixed-depth prefixes, not the whole list:

   ```
   check-pending-questions.py:258  notify_macos       titles[:3]
   check-pending-questions.py:327  notify_discord_dm  questions[:5]
   check-pending-questions.py:310  notify_voice       unsliced
   ```

   With 36 open items, anything at index ≥ 5 renders on **voice only** — and voice is usually not
   connected. Measured 2026-08-20: the Google-Drive-mirroring-the-live-repo question, filed that
   day and the highest-stakes item on the list, sat at **position 35 of 36** and reached no surface
   the owner reads, while `len(q)` honestly reported 36 the whole time. Sutando-rui hit the same
   thing independently: a PR needing ~30 seconds of owner time sat at position 12 for days, blocked
   not on review or code but on a rendering slice.

   **So: a fixed-depth prefix over an append-ordered list makes POSITION a priority signal whether
   or not anyone intended one, and appending asserts the lowest one by construction.** Decide
   placement deliberately at write time. If the new question outranks what is already at the top,
   put it at the top; if it does not, you have just decided it can wait — say so to yourself, not
   by accident.

   **Assert the right invariant for the edit you actually made** — the count discriminates
   differently per operation, and the wrong choice passes while the entry is gone:

   | edit | assert |
   |---|---|
   | new question | count went **up**, and the title matches (see below) |
   | reorder / promote | count **unchanged**, and the entry is now inside the rendered prefix |
   | fold two into one | count went **down by exactly the number folded**, AND the folded id appears in the survivor, AND no standalone entry for it remains — a fold that *lost* an entry shows the same count |

   I filed two questions this way on 2026-08-02 (the ep007 spine pick, and an ag2space room-join request) and **both were invisible**: the reader stayed at 22 while the file grew. Moving them above the divider took it to 24. **This is the exact defect PR #2521 fixes in `auth-preflight-gate.sh`** — which I reviewed, fixed an ABA race in, and pushed the same afternoon I committed the bug by hand, twice.

   It reports success in every cheap way: bytes land, the path is right, nothing errors, the file grows. **Only calling the reader shows the zero.** So after writing, assert it:
   ```bash
   python3 -c "import importlib.util;s=importlib.util.spec_from_file_location('c','src/check-pending-questions.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);q=m.get_waiting_questions();print(len(q), sum('<distinctive phrase from your TITLE>' in (x.get('title') or '') for x in q))"
   ```
   **Match on `title`, and check that the COUNT went up — not `str(x)`.** ⚠ 2026-08-13: the
   substring-anywhere form above this line passed while the entry was **swallowed into the
   neighbouring section's body**, because a merged section still contains your text. The reader
   splits on `##` ONLY; a `###` heading is body text, not a new question. Tell: a purely additive
   edit (`git diff --numstat` = N/0) that leaves the count UNCHANGED. I saw that delta=0, explained
   it away as a stale count, and only a title-level check showed the zero. The count is the
   discriminator; the substring cannot fail the way this actually fails.

   **⚠ A COUNTED question can still be INVISIBLE — assert POSITION too (2026-08-20).** The count
   rising proves membership, not visibility. Waiting order is FILE order, so "insert above the
   `# Resolved` divider" — the rule that makes a question counted at all — lands it at the BOTTOM of
   the visible list. The two rules pull opposite ways. Only two consumers render anything, and both
   take an ordered prefix: `notify_macos` shows `titles[:3]` and the proactive DM body shows
   `questions[:5]` (hence `VISIBLE_PREFIX = 5` in `src/check-pending-questions.py`). **Positions 6+
   render nowhere** — they exist only in the file and the web UI's Questions tab. Measured on a live
   host: `VISIBLE_PREFIX=5; waiting=34; rendered nowhere = 29 of 34`, and the question filed that
   pass sat at **34/34** while the assertion documented above printed `34 1` — a pass. Extend the
   proof to position:
   ```bash
   python3 -c "import importlib.util;s=importlib.util.spec_from_file_location('c','src/check-pending-questions.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);q=m.get_waiting_questions();i=next(k for k,x in enumerate(q,1) if '<distinctive phrase from your TITLE>' in (x.get('title') or ''));assert i <= m.VISIBLE_PREFIX, f'filed at {i}/{len(q)} - below the fold, renders nowhere'"
   ```
   If it lands below the fold and it genuinely needs the owner, **move it up** — do not file a second
   question about the first one being unread. Promotion is self-announcing: `notify_key` hashes the
   visible-ordered prefix (#3004), so changing the top 5 defeats the cooldown by construction and the
   next fire notifies.

9. **Ensure the streaming watcher is running.** **Read the `task-watcher` probe from the `health-check.py` run you already did in step 3 — do not re-derive liveness here.** That probe is the authoritative signal: it enumerates real watcher process trees (`_watcher_trees()` in `src/health-check.py`) and reports which of four states holds. Act on the state it names:

   | probe says | action |
   |---|---|
   | `ok` | nothing to do. |
   | watcher(s) running with **no PID sentinel** (orphaned) | **Do NOT start another** — that is what creates the duplicate. Stop the pids it names, then start exactly one. |
   | sentinel pid dead but **other watcher(s) still run** | same: stop the named pids, then start one. |
   | not running (no sentinel, no trees) / pid dead with none running | start one with the `Monitor` tool: `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`. |

   **A missing sentinel is UNKNOWN, not DEAD.** The sentinel is written once at startup (`watch-tasks-stream.sh` line ~316) and removed by cleanup only when the content still matches that pid, so an absent file cannot distinguish "no watcher" from "a live watcher whose file was removed". Measured 2026-08-07 on a live core: the watcher had held one pid for ~5h, was **functioning** (it emitted `TASK_FILE:` for a probe written during the check), and the sentinel was absent from disk entirely. The instruction this step used to carry — *missing OR dead → restart* — would have attached a second watcher to that live one, and both then emit every task, so each task gets processed twice. `health-check.py` names this failure directly at its `task-watcher` probe: restarting on a dead-looking sentinel "is what produces the duplicates in the first place."

   When notifications arrive (`TASK_FILE: <basename>`), Read the named file. Each event represents one new task — process all queued tasks before continuing.

   **Don't hand-roll a process check to second-guess the probe.** `pgrep -f watch-tasks` / `ps | grep watch-tasks-stream` both match the wrapper shell that runs the check (its own argv contains the search string), so they return a pid for a transient subshell or pick the wrapper instead of the watcher — an attempt at this on 2026-08-07 reported rc=1 with the watcher demonstrably alive. `_watcher_trees()` already solves this by scoping to process trees; use its verdict via the probe.

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
