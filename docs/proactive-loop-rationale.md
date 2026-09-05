# Proactive loop — rationale and measurements

Every paragraph below is the verbatim text that lived in `skills/proactive-loop/SKILL.md` until
it was reduced to commands (sonichi/sutando PR: commands, not lessons). The skill now names a step
number; the matching section here carries the measurement, the incident and the reasoning. This
file is loaded on demand, never per pass. Do not add to the skill what belongs here.

---

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

   **Windows:** the `Monitor` tool is unavailable, so `src/startup.ps1` starts
   `src/task-dispatcher.ps1`, an external `FileSystemWatcher` that invokes `claude --print` for each
   task. The core handles cron-driven autonomous work; starting another watcher would duplicate task
   processing.

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
   Then run `python3 skills/proactive-loop/scripts/claude-quota-cadence.py --json`. The helper
   preserves the configured `main-loop` cron while 7-day utilization is below 80%, selects
   `*/30 * * * *` at or above 80%, and conservatively selects 30 minutes when quota telemetry is
   missing, stale, rejected, or not authoritative for this routed core. Compare `effective_cron`
   with the `/proactive-loop` job in `CronList`. Only when they differ, capture the old job ID,
   `CronCreate` the new job with `prompt: "/proactive-loop"` and `cron: <effective_cron>`, and
   confirm the new ID and cadence in `CronList` **before** deleting anything. Then `CronDelete`
   the captured old ID and confirm exactly one `/proactive-loop` job remains at the effective
   cadence. If create or confirmation fails, retain the old job and stop loudly; a brief duplicate
   is recoverable, while deleting the only loop driver is not. Do not edit
   `crons.json`: its cron is the normal cadence restored automatically after the 7-day reset.

   **⚠ DO NOT HAND-SELECT THE TIER. Pipe it through the helper (added 2026-09-01 after I inverted
   the comparison and printed FULL on a MEDIUM budget):**

   ```bash
   python3 $CLAUDE_CONFIG_DIR/skills/quota-tracker/scripts/read-quota.py \
     | python3 skills/proactive-loop/scripts/quota-tier.py
   # -> 5h ... -> FULL / 7d ... -> MEDIUM / TIER MEDIUM (bound by 7d)  sustainable Nx CURRENT pace
   ```

   It tiers each window by its OWN rule, selects with `max` over an ordered scale (`min` returns the
   LEAST restrictive — that was the bug), infers reset years with a bounds check, and refuses rather
   than guessing. 15 tests; the discriminating ones are the MIXED pairs, since same-tier pairs pass
   under both `min` and `max`. The prose below stays as the rationale — read it to understand the
   two rules, but do not execute it by hand.
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
- **(d2) Intentional stop**: `python3 src/shutdown.py check` exits 0. Someone stopped this core on
  purpose (`restart.sh --stop-only`, the menu-bar Stop). Finish the task in hand, start no new one,
  write `{"status":"idle"}` and end the loop cleanly — do not treat it as a crash and do not relaunch.
  Which paths set it, which clear it, and why a plain restart clears it:
  [`docs/graceful-shutdown.md`](graceful-shutdown.md).
- **(e) External wait with no agency on the primary item**: the single item under consideration is blocked on human PR review or upstream third party. Only gates THAT item — other menu items remain fair game.

**Blocker ≠ stop.** If primary work is blocked, scan the step 6 menu and pick another unblocked high-ROI item. Idling because "nothing to do" is laziness, not a skip.

## The numbered loop

1. **Check for tasks.** Look in `tasks/` for voice / Discord / Telegram / phone tasks. Look at `context-drop.txt` for context drops. Process anything found — execute the task, write results to `results/`.
   - When the core itself consumes a task, move the source from `tasks/<id>.txt` to
     `tasks/archive/<id>.txt` after writing its result. Long-lived bridges and the Windows dispatcher
     archive their own claims. A source left in `tasks/` is re-emitted when a watcher restarts because
     its seen set is process-local.
   - **Access control:** If the task has `access_tier: other` or `access_tier: team`, delegate to a sandboxed agent. Do NOT process non-owner tasks with your full capabilities. Write the sandboxed output to results.
   - Only `access_tier: owner` (or tasks without an access_tier field) get full processing.
   - **Thread consolidation:** when several tasks in a short window are the same continuation thought (e.g. voice over-delegating "yes, right, this is useful…" as 3 separate tasks), put the FULL reply in the latest task's result and put `[deduped: task-<latest-id>]` in each earlier task's result.

     **⚠ THE TARGET MUST ACTUALLY DELIVER. `[deduped: X]` onto a `[no-send]` X is a contradiction** — it says "the reply is in X" where X says "send nothing" — and the failure is invisible until the bridge announces it INTO THE ROOM, naming an internal task id the peer cannot resolve. Measured 2026-09-01: three collaborator notices closed that way produced a DELIVERED outbox item reading *"This was folded into `task-<internal id>`, which delivered nothing"*, and the peer spent a 519-task sweep hunting an id that never existed on their side. All-time on this host: **68 such pairs on disk**. ⚠ I first published "12 of which reached a room" — WRONG, and the error is instructive: I grepped my outbox for the phrase, which matched my own replies QUOTING it, so my write-up inflated its own count. Corrected: 5 genuine bridge notices, of which 3 had targets that DID deliver (the notice was a false alarm), so **2 genuinely delivered nothing — and neither owed a reply**. The harm was five confusing room notices, not lost messages. **A grep for a defect's own wording counts the documentation of the defect.**

     `[deduped: A]` where A is itself `[deduped: B]` resolves to no reply just as completely (found by @yixuan-ag2 against their tree).

     **⚠ AND THE WHOLE JUDGEMENT BELONGS TO `src/result_markers.py`, WHICH ALREADY HAD IT.** `dedup_holder_delivered()` and `dedup_decision()` ship in the repo: the bridge already detects this, REQUEUES the asking task with a "holder delivered nothing, answer directly" reason, and only reports to the room after a requeue ALSO failed. So the room notice is the third step, not the first. I built a checker that re-implemented that policy and it drifted TWICE in one night — a hand-rolled `[no-send]` test called a `[REPLIED]` holder delivered, and hand-rolled chain-WALKING (`a -> b -> real reply` => clean) was **more permissive than production**, because `[deduped:]` is itself a skip action so the bridge requeues a chained holder and never walks it. A guard that clears what the bridge rejects is worse than no guard. The checker now delegates and refuses (exit 2) if the module cannot be imported, rather than falling back to a weaker local rule. Corpus went 68 -> 88 findings on the fix.

     ```bash
     python3 skills/proactive-loop/scripts/check-dedup-targets.py "$WORKSPACE/results/<file>.txt"
     # 0 clean · 1 the dedup resolves to nothing · 2 could not answer (NOT a green light)
     ```

     If every message in the group is a notice needing no reply, use `[no-send]` on **all** of them — never `[deduped:]` pointing at one. Guarded by `tests/proactive-loop-check-dedup-targets.test.py` (11 tests; mutations verified red). The bridge silently archives the deduped ones — no voice cascade, no DM duplicates. See CLAUDE.md "Result-body protocol markers" for the full marker list.

   **⚠ CLOSE THE PASS BY ASKING THE QUEUE, NOT YOUR MEMORY.** Before writing the idle
   status, run:

   ```bash
   python3 scripts/unanswered-tasks.py --workspace "$WORKSPACE"   # rc=1 => a task got no result
   ```

   A task file stays in `tasks/` until a result is written and the bridge archives it, so
   the queue already *is* the record of what is unanswered — nothing reads it at the end of
   a pass. The miss is invisible from the inside: the work gets done, the reply is composed
   in the transcript, the terminal shows it, and only the queue disagrees. Measured five
   times in one session, caught every time by re-listing by hand and never once by recall —
   which is why this is a command and not a reminder. It fires only on tasks older than
   `--min-age-sec` (default 120), so a task still in flight is never flagged.

2. **Check pending questions.** Read the **per-host** `pending-questions.md` — `<workspace>/hosts/<hostname>/pending-questions.md` (`<hostname>` = `bash scripts/sutando-config.sh host-label`; this is the F1 per-host location, carried by `hosts/*/`, and where `personal_path("pending-questions.md")` resolves). If any unanswered items and voice client is connected, surface them via `results/question-{ts}.txt`. Also send a macOS notification.

3. **Check system health.** Run `python3 src/health-check.py`. If issues found, fix what you can (`--fix` flag), note what you can't.

   **⚠ A WARN IS A POINTER INTO THE RECORD, NOT NEW INFORMATION. Grep the PQ before investigating one.** Health-check warns are chronic by construction: they re-fire every pass until an owner decision lands, so the ones that survive are precisely the ones already investigated and parked. The warn text looks new every time and carries no memory of having been read — that mismatch is the whole trap.

   **Grep the SUBJECT, not the probe name.** A warn is named for its *detector*
   (`memory-sync`, `daily-cron-punctuality`); a question is filed under the *subject of the decision*
   (`unfiled-findings-backlog`, `example-digest`). Nothing keeps those vocabularies aligned, so the
   name you already know is the one least likely to hit. Measured across five live warns: the probe
   name hit **2 of 5**, an entity name taken from the warn TEXT hit **5 of 5** — and on `memory-sync`,
   the case this rule was written for, the probe name returns **0** while the write-up sits under
   `unfiled-findings-backlog`.

   ```bash
   # token = an entity from the warn TEXT (a path, filename, host, command), not the probe name
   H="$WORKSPACE/hosts/$(bash scripts/sutando-config.sh host-label)"
   grep -in "<subject-token>" "$H/pending-questions.md" "$H/current-track.md" | head
   ```

   **Grep BOTH parking files.** Warns get parked wherever the pass that triaged them was writing —
   a second core measured two of its own parked analyses in `current-track.md` (a health-warning
   triage heading, a cron-cause note), where a PQ-only grep misses them by construction.

   **A zero means "try another token", not "nothing is filed."** Two or three tokens from the warn
   text, then — before concluding absence — enumerate the headings instead of querying:
   `grep -n '^## ' "$H"/*.md`. Reading ~25 headings takes seconds and cannot miss due to token
   choice, which is exactly how a self-chosen token fails: the suspicion generates the tokens, and
   the answer sits under a heading the suspicion never touches. Then investigate. One call each, before any investigation costing more than a couple of tool
   calls. It either returns nothing or hands you your own prior write-up — with the measurements, the mechanism, and usually the proposed fix already in it. When it hits: **extend it with what is genuinely new, or say plainly that nothing is new.** Re-filing a weaker duplicate is the failure mode, and surfacing one to the owner as a discovery makes them read the same thing twice.

   This lives in the loop file rather than only in a memory because a memory loads when RECALLED while this file loads EVERY PASS. The rule already existed, stated sharply, and still failed repeatedly — placement was the defect, not precision.

3.4. **⚠ THE ZERO-RESULT RULE — generalised from step 3, because it is not about warns (added
   2026-08-28 after NINE instances in ONE session, each confirmed in that night's build_log by a
   distinctive token, not by a regex over the log).**

   Step 3 says a zero PQ-grep means "try another token". **The same holds for EVERY probe**, and the
   nine were: a dict filtered by `startswith('http')` when keys were `repo#N` (-> "0 tracked PRs",
   there were 26); `git ls-tree | head -5` (-> a file "absent" from a 75-entry dir); `git diff` on a
   STAGED file (-> 0, vacuously, forever); a zsh `"$H:path"` eaten as a parameter modifier; grepping
   `def read_gateway_verdict` when it is an ALIAS of `read_verdict`; a regex `return [2-5]` against
   code that says `refusal_rc or 5`; a 90-line WINDOW read of a function whose wiring was further
   down; `ps | grep` matching its own argv (-> "5 processes", 3 were the check itself); and a
   block-split of `git log --name-only` that printed `.gitignore` as a commit header.

   **Every one returned a clean, quotable, WRONG answer.** None errored. Several were published.

   **So, before reporting any empty/zero result as a fact about the world:**
   - **Name what would have to be true for a NON-zero.** If you cannot, the probe is not a measurement.
   - **Run the positive control** — the same probe against a case you KNOW is present. A probe that
     cannot produce a hit scores 0 by construction and certifies nothing.
   - **Suspect SCOPE first**: a `head`/`tail`/`--limit`, a directory searched non-recursively, a window
     instead of a file, a single package instead of the tree. Absence inside a bound is a fact about
     the bound.
   - **Suspect the SHAPE second**: print one record before filtering a collection (`print(rows[0])`),
     and check an alias is not the definition.
   - **Never verify in zsh.** A blank where a number belongs is a SYNTAX result; re-run in python3.

   **⚠ AND IT FIRES BEFORE TELLING THE OWNER SOMETHING IS TRUE OF THE SYSTEM — not only before
   reporting a zero (added 2026-09-01 after FIVE instances in one night).** Step 3 scopes the grep to
   health warns and 3.4 scopes it to probes; neither covers *"I noticed X about how this system is
   wired, worth you knowing"*. That sentence is the highest-cost one to get wrong — it reaches the
   owner — and it is the one with no gate in front of it.

   Measured the same night: I told the owner `workspace/build_log.md` is outside the vault carrier
   set and that its backup "depends on a mirror running, worth knowing rather than my quietly
   assuming." **My own `current-track.md` already carried that claim AND its retraction** — filed
   2026-08-29, retracted the same day with byte-identical remote proof, plus the reason the root path
   is excluded ON PURPOSE (it collided across hosts; `sync-workspace.sh:846-853` snapshots it per-host
   instead). The step-3 grep found all of it in ONE call, the next pass, after I had already said it.

   The other four that night: re-deriving a footer discriminator I authored 5 days earlier; hunting a
   doc the instrument I was about to run already prints; hunting a `--folder` the memory that failure
   routes to already documented; and appending a memory section that was already there — caught only
   because the assertion was `== 1` and returned 2.

   ⇒ **Before any sentence to the owner of the form "X is how this system behaves", run the step-3
   grep on X's own nouns first.** One call. The record is usually ahead of you, and it often contains
   your own retraction of exactly what you are about to say.

   **⚠ THAT RULE HAD NO MECHANISM AND FAILED AGAIN 2026-09-01.** I told the owner `hosts/` holds two
   subtrees and called it host-label drift worth settling — measured, diagnosed and filed since
   **2026-08-28** under `[host-subtrees-false-red-20260828]`, complete with a prepared patch. She
   read it twice. The 3.4 clause above was loaded on that pass, as it is on every pass; prose in a
   file I read is not a gate.

   ⇒ **RUN IT. It is one command and it takes a second:**

   ```bash
   python3 skills/proactive-loop/scripts/warn-already-triaged.py --claim "<the sentence you are about to say>"
   # exit 1 = already parked, with file:line -> READ IT, then extend or say nothing is new
   # exit 0 = NO TOKEN MATCHED. That is not proof of absence -- the tool's own message says
   #          "OR every token missed". Before proceeding, do step 3's fallback:
   #          grep -n '^## ' "$H"/*.md  and read the ~25 headings. Enumerating cannot miss
   #          the way a self-chosen token does, and the suspicion never generates the token
   #          the answer is filed under.
   # exit 2 = could not answer (no parking files / empty claim) -> NOT a green light
   ```

   Same `tokens()` + search the warn path uses, so the two cannot drift. Verified against the
   failure that produced it: the exact sentence I sent her returns exit 1 pointing at the parked
   entry. Guarded by `tests/proactive-loop-warn-already-triaged.test.py` (10 tests; the discriminating one asserts
   the verdict flips when the subject is redacted from **both** parking files — a one-file redaction
   leaves it firing and reads as insensitivity that is not there).

   This is step 3's rule with the noun changed. It sits here rather than in a memory for the reason
   step 3 already gives: a memory loads when RECALLED, this file loads EVERY PASS — and all nine
   happened on a night when the memory existed and was loaded.

3.45. **⚠ BEFORE `gh issue create`, RUN THE DUPLICATE GATE — CHAIN IT, do not just read it.**
   Step 3.4 guards a claim you are about to make to the owner. This is the same rule for an artifact
   you are about to file on GitHub, and it needs its own gate because the parking files it searches
   are not GitHub.

   ```bash
   python3 skills/proactive-loop/scripts/gh-duplicate-check.py \
       --repo <owner/name> --title "<the title you are about to file>" \
     && gh issue create --repo <owner/name> --title "..." --body-file <f>
   # 0 no candidate -> the `&&` lets the create run · 1 DO NOT FILE, names the candidates
   # 2 could not answer (search failed / no tokens / a decision parameter below 1) -> NOT a green light
   ```

   **The `&&` is the whole mechanism.** On 2026-09-04 I filed #3889 duplicating #3862 after running
   the search in the SAME command block as the create: the answer printed, nothing consumed it, and
   the create ran anyway. A check whose result is not the action's precondition is decoration.

   It fails closed — a failed search returns "did not search", never "found nothing"; partial
   coverage with no hits is exit 2; and `--max-queries 0` / `--min-overlap 0` are refused rather than
   scoring a vacuous clean bill. A rc=0 still says in words that it is not proof of absence: an
   `in:title` search cannot see a duplicate worded differently in its title.

   Guarded by `tests/proactive-loop-gh-duplicate-check.test.py`, which pins THIS wiring as well as
   the tool — an unreferenced gate runs never, which is worse than one described only in prose.

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

3.6. **Re-run my own tool suites when a tool changed — the instruments this loop quotes are
   not otherwise tested.** `merge-gate.py`, `check-dedup-targets.py`, `warn-already-triaged.py`,
   `memory-index-budget.py`, `idle-held.py` all have suites; **nothing re-ran them.** Measured
   2026-09-01: two were broken. `notify_reviewers.py` did not PARSE — I broke it that morning
   marking it superseded, and never re-ran its suite. `merge-gate-gate.test.py` had been dying at
   check 3 of 124 on a fixture that drifted behind the code, so **121 assertions on the instrument
   every shepherd sweep quotes had gone unexecuted**.

   ```bash
   python3 skills/proactive-loop/scripts/tool-suites-check.py --workspace "$WORKSPACE" --repo "$PWD"
   # fresh -> two stat() calls, prints one line · 0 all pass · 1 a suite FAILED · 2 cannot answer
   ```

   **The trigger, not the cadence, is what closes the gap.** A daily run would still have let that
   morning's edit sit green until the next day. This fires when any tool or suite is NEWER than the
   last green run, and otherwise only after 24h, so an edited tool cannot keep a stale pass.
   Controls verified: unchanged -> skip; `touch` one tool -> runs; break one tool -> exit 1 naming
   the suite; restore -> exit 0.

   The six suites ship as `tests/proactive-loop-*.test.py` (CI discovers only `tests/*.test.py`;
   `tests/ci-covers-every-python-test.test.py` refuses a suite anywhere else). They sit outside
   `$WORKSPACE/scripts`, so declare them (repo-relative) in
   `$WORKSPACE/hosts/<host>/tool-suites-extra.json` — the vault carries `hosts/*/` and does not
   carry `state/`, so a declaration left under `state/` is unbacked-up, and losing it disables its
   suites silently. A `state/` copy is still read when no carried one exists.
   — `{"suites": ["tests/proactive-loop-idle-held.test.py", ...]}` — to put them under the same
   changed-since-last-green trigger; a declared path that does not exist is exit 2, never a skip.

   ⚠ It invokes each suite with **no extra argv**. A `unittest`-based suite reads `argv[1]` as a
   test-NAME selector, so passing a repo path makes it error with `AttributeError: module
   '__main__' has no attribute '/Users/...'` — indistinguishable from a real failure until you vary
   the harness. Running this sweep by hand that way produced **four false failures** out of six.

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

   Classify this pass: **substantive** (processed a task, shipped a fix/PR, filed a memory, posted to owner) or **no-op** (nothing owner-visible happened). Record it with the script — **do not maintain `state/idle-streak.json` by hand.** `record_outcome()` owns `streak` and the cumulative totals under a lock, so a second writer double-counts every pass and the drift is silent:

   ```bash
   python3 skills/proactive-loop/scripts/idle-surface-hash.py \
     --state "$WORKSPACE/state/idle-streak.json" --pass-outcome substantive|noop
   ```

   It returns before touching stdin, so it is safe under cron. `last_surfaced_hash` is a different field with a different contract and is still written by the `--commit` path below.

   On the **first no-op** of a run (`streak >= 1`):
   1. **Generate, don't idle** — first widen the menu and actually try to produce a tangible artifact (peer-PR review, regression grep, parity verify, research, memory curation, own-PR CI). Gated ≠ nothing-to-do. Only if genuinely all-gated go to step 2.
   2. **Surface once per changed set** — build the held-list as `(item_id, gated_on)` pairs, where
   `gated_on` is a short stable token (`owner`, `ci`, `upstream`, `peer-review`) — it is reduced
   to its leading token, so re-describing one blocker cannot make a second key. ⚠ **`item_id`
   is NOT reduced**: use a stable identifier (a PR number, a fixed slug), never a rendered
   description — an id carrying a live count re-hashes on a change nobody needs told about.
   Hash it with:

   ```bash
   # 1. COMPUTE — no --commit, so nothing is stamped and you are free to decline.
   echo '[["3166","owner"],["3274","owner"]]' |
     python3 skills/proactive-loop/scripts/idle-surface-hash.py \
       --state "$WORKSPACE/state/idle-streak.json"
   # -> post <hash>   (changed set: surface it)   |   quiet <hash>  (unchanged)

   # 2. Post the message. THEN, and only then, stamp the set as surfaced:
   echo '[["3166","owner"],["3274","owner"]]' |
     python3 skills/proactive-loop/scripts/idle-surface-hash.py \
       --state "$WORKSPACE/state/idle-streak.json" --commit
   ```

   ⚠⚠ **AND DO NOT BUILD THE LIST EITHER — pipe it from the record.** The hash script takes the
   whole set from its caller, and an agent handed that interface builds the set from RECALL. A
   recall-built list is a different set wearing the same name, so the hash says "post" and
   `--commit` then overwrites the legitimate baseline with it. That happened **three times** on this
   host; the third replaced an 18-item record with 4 ids remembered under the wrong names
   (`3198` for `sutando-3198`, `cinny-690` for `cinny-700`). Every one of those passes had the
   warning in the very file it was writing to — the list gets BUILT before the file gets READ, which
   is why prose cannot fix it.

   ```bash
   # 1. COMPUTE — no --write and no --commit, so nothing is persisted and nothing is
   #    stamped. `idle-held.py` prints the resulting list before it consults --write.
   python3 skills/proactive-loop/scripts/idle-held.py --state "$WORKSPACE/state/idle-streak.json" \
       --remove <id> --reason "<why>" --add <id>:<gate> \
     | python3 skills/proactive-loop/scripts/idle-surface-hash.py \
         --state "$WORKSPACE/state/idle-streak.json"
   # -> post <hash>   (changed set: surface it)   |   quiet <hash>  (unchanged)

   # 2. Post the message. THEN persist the ops and stamp the set as surfaced:
   python3 skills/proactive-loop/scripts/idle-held.py --state "$WORKSPACE/state/idle-streak.json" \
       --remove <id> --reason "<why>" --add <id>:<gate> --write \
     | python3 skills/proactive-loop/scripts/idle-surface-hash.py \
         --state "$WORKSPACE/state/idle-streak.json" --commit
   ```

   Both runs read the same unmutated state, so they print the same list and hash identically;
   deferring `--write` too means a declined send leaves neither the stamp nor the ops behind.

   It reads `held_item_ids` from the state file and applies explicit ops; **there is no interface
   that accepts a whole list**, so a recall-built set cannot be expressed. A `--remove` of an id the
   record does not hold is refused and the near-miss named; a removal without `--reason` is refused,
   because a silent shrink is the failure that corrupted the baseline. 48 assertions, 5 mutations red.
   ⚠ Nothing else writes `held_item_ids` — verified across 10,024 files in both trees, where the key
   appears only in the state file, in prose records and in one patch. It was hand-maintained, which
   is exactly why it drifted.

   **⚠ AND THE NOTES DRIFT TOO — `held_item_notes` has no guard, unlike the ids.** A note that
   carries `<branch> @ <sha>` is a COPY of a fact git owns, so it goes stale silently. Measured
   2026-09-01: two notes held shas that `current-track.md` had ALREADY corrected — a third record
   of one fact, disagreeing with both. Audit them against git, not against another note:

   ```bash
   python3 skills/proactive-loop/scripts/idle-held.py --state "$WORKSPACE/state/idle-streak.json" \
       --audit-notes "$PWD"        # 0 all match git · 1 a note disagrees, named
   ```

   **And re-check the held items themselves, not just their shas.** The same pass found `ds-pr-12`
   still listed as "waiting only on the owner merge" **17 hours after it merged** — an FYI surface
   would have reported a blocker that no longer existed. A PR-backed held item is one `gh pr view`
   away from being checkable; retire it through `--remove <id> --reason "<why>"`.

   ⚠ **Two steps, because `--commit` stamps at HASH time, not at SEND time.** The single-call form
   is correct only when the send is unconditional — and it is not: the guardrails below tell you to
   stay quiet when the owner is away, and step 1 tells you to keep generating instead. Committing
   before deciding records a delivery that never happened, and the next genuinely-new change to the
   same set is then deduped into silence — the failure this guard exists to prevent, pointing the
   wrong way. The state keeps no history, so the stamp cannot be undone.

   ⚠ **Do not compute this hash yourself.** The rule used to live here as "sha1 the held-list", and an
   agent handed that instruction hashes the sentence it was about to send — so re-wording the same
   items yields a new hash every pass and the guard never dedups anything. A guard described in prose
   is not unimplemented; it is implemented with the executor's default as its body.

   If `hash != last_surfaced_hash`: post ONE concise "here's what's held / needs you (FYI, not a block)" line to the **owner's primary channel** (see `PERSONAL_CLAUDE.md` channel routing — NOT the `#bot2bot` coord channel), then set `last_surfaced_hash`. If `hash == last_surfaced_hash`: stay quiet **only if the owner is away/asleep** (`last-owner-activity.json` older than ~30 min); if he's been active in the last ~30 min, never go dark — drop a one-line progress/activity signal to his channel anyway.

   **Guardrails (all owner-corrected):** the surface is a non-blocking FYI footnote — NEVER a new wait-state ("awaiting your go" is not a reason to pause; keep doing the next unblocked thing). Don't spam: one signal per changed set / per work-shift, not per file. Presence is the discriminator: recently-active → never silent; genuinely-away → dedup-quiet is fine.

6.7. **Failure closure = mechanism, never a filed lesson (owner-durable 2026-08-27: "why do I
   need to keep reminding you to make durable fix").** Every report of a failure — your own or one
   a reviewer/owner caught — ends with exactly one of: (a) the MECHANISM that makes the recurrence
   structurally impossible (a gate, a generated row, a checker), linked; or (b) the explicit
   sentence "no mechanism exists, because X." A lesson written to a log or memory is not a third
   option: a memory loads when RECALLED, a mechanism runs unconditionally — and this file loads
   every pass, which is why the rule lives HERE and not in the memory that first recorded it.
   Measured the day it was written: two mechanisms (sutando-skills#440, #441) each existed within
   an hour of the owner's prompt, so the cost was never the building — only the definition of done.

7. **Update `$WORKSPACE/build_log.md`** — mark what changed, update statuses, note what's next.

   **⚠ THEN ASSERT THE WRITE LANDED — three misses in one session, 2026-08-22.** The append
   reports success in every cheap way and still does not happen:

   ```
   printf '...' "$(date -u ...)"          # redirect dropped: renders to TERMINAL, reads as success
   cat >> "$W/build_log.md" <<'EOF' ...   # landed
   echo logged                            # proves the ECHO ran, never that the APPEND did
   ```

   Measured the same day: a `printf` whose `>> build_log.md` was omitted printed the entry to
   stdout and looked identical to a successful write; a whole diagnosis was reported to the owner
   and never written (`grep -c` returned **0** a pass later); and two completed owner tasks were
   left with no result file at all. In every case the terminal showed the text.

   **So close the write by reading it back, exactly as step 8 already does for the questions
   reader** — same shape, different file. **But do NOT re-type the phrase to search for.** A probe
   typed a second time from memory drifts from the text it is checking, and then fails the same way
   the write fails. Measured on a peer node the same day: an audit of seven appends reported
   **1 MISSING** because the probe used wording from the *commit message* while the entry said
   something else. False MISSING is the dangerous polarity — it invites redoing work already done,
   and a check that cries wolf gets demoted to the category that never fires.

   **Define the marker ONCE and assert on the same variable**, so the probe cannot drift from its
   subject and a dropped redirect cannot satisfy it:

   ```python
   MARK  = f"step7-{uuid.uuid4().hex[:12]}"  # UNIQUE BY CONSTRUCTION, not by circumstance
   entry = f"### {ts} — ...  [{MARK}]\n..."  # interpolated into what is WRITTEN
   with open(path, "a") as f:                # O_APPEND — NEVER read_text() + write_text()
       f.write(entry); f.flush(); os.fsync(f.fileno())
   assert path.read_text().count(MARK) == 1  # reads the FILE, never the terminal
   ```

   **⚠ NEVER close this write with `p.write_text(p.read_text() + entry)`.** `build_log.md` is
   shared, synced, multi-writer state and is append-only by contract. A read-modify-replace lets any
   append landing between the read and the write be **silently erased** — and the erasing writer's
   own `count(MARK) == 1` still passes, because its marker is present in the file it just truncated.
   That is the worst failure this section can have: the step that exists to certify a write becomes
   the step that destroys another host's. Measured: two writers both reading `base\n` leave final
   content `base\nB\n`, with A gone and B's assertion green. Use `O_APPEND` (atomic per write) or a
   shared lock — never whole-file concatenate-and-replace.

   **The marker must be unique per write, and `== 1` is why.** A literal constant passes on pass 1
   and then fails forever: the marker survives in the log, so pass 2 finds it twice and a *correct*
   append fails its own assertion — inside a loop step, where `assert` raises and takes the rest of
   the pass with it. With `ts` in the marker, `== 1` means *this entry landed exactly once*; with a
   constant it means *this log has been written to exactly once ever*, which is a different claim
   and almost always false.

   **Do not reach for a timestamp here.** `f"step7-{ts}"` is unique only by *circumstance* — it
   holds while the clock is fine-grained enough and the writes are far enough apart, and collides
   the moment two appends land in the same tick. At minute granularity that is an ordinary loop
   pass. A random marker has no such premise. And confirm the check can still FAIL: re-append the
   same marker deliberately and watch `count` reach 2, or you have a probe that passes by
   construction, which certifies nothing.

   `== 1`, not `> 0` — a 300 KB log may already contain the phrase somewhere else. And check that
   the probe can produce a positive at all: a marker matching nothing scores 0 by construction and
   cannot fail, which is a control that certifies nothing.

   `echo logged` / `echo closed` is not this check. It asserts the *last* command in the chain
   ran, which is true even when the append was the one that silently went elsewhere.

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

7.5. **⚠ BEFORE ADDING A ROW TO `MEMORY.md`, ASK WHAT IT COSTS — the index is a BYTE
   PREFIX and it is 181 B from the cut (2026-09-01).** The session reads `MEMORY.md` up to
   25,000 B / 200 lines; rows past that are dropped **silently**, while every memory file still
   looks perfect on disk. `health-check.py` reports this AFTER the fact, so the first signal that
   a lesson stopped loading is a warn on a later pass, and it never names the casualty.

   ```bash
   python3 skills/proactive-loop/scripts/memory-index-budget.py --adding "<the exact row you are about to add>"
   # 0 safe · 1 REFUSE — it names the row that would drop, or says the addition itself won't load
   #                     · 2 cannot answer (health-check not importable) — NOT a green light
   ```

   **Headroom is the wrong question and that is why this is a script.** "181 B remaining" reads
   like a budget you may spend and cannot name what spending it costs; the tool asks which rows
   load now, which load after, and what is in the difference. Measured on the live index with a
   median 239 B row: appending refuses because **the new row never loads**, and inserting at the
   top refuses because it evicts a *different* row (named). Same addition, two distinct casualties
   — a headroom number shows neither.

   It DELEGATES to `health-check.py` (`_index_effective_text` + `_index_loaded_prefix` +
   `MEMORY_INDEX_LOAD_BYTES`) and refuses rather than falling back to a private copy of the limit,
   because a guard that measures differently from the probe it guards clears writes that probe
   will later condemn — the drift that made the dedup checker worse than no checker. 13 tests,
   4 mutations verified red, including one that installs exactly that silent fallback.

   **On a refusal, free room FIRST — and run `scripts/memory-hub-containment.py` before trimming**,
   so a row you remove is still carried by its hub. Which rows may go is the owner's call
   (`pending-questions.md` -> "MEMORY.md byte budget"); the guard's job is only to stop the write
   that would decide it by accident.

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

   **⚠ STOP ONLY THE PIDS THE PROBE ITSELF CLASSIFIES AS OWNERLESS.** The sentinel is
   single-valued, but a pool legitimately runs **one watcher per core**, so N-1 correct watchers
   always read as "untracked duplicates" and which pid holds the sentinel is arbitrary.

   The probe does this classification for you, same-host, from the local process table — but only in
   *some* of its branches. **Act only on a verdict that presents owned and ownerless as two
   separately-labelled groups.** Key on that structure, never on wording:

   - two groups, one of them ownerless → stop exactly the ownerless roots, leave the rest.
   - two groups, none ownerless (every untracked tree session-owned) → **change nothing**.
   - **one undifferentiated list of pids → change nothing** and say so, *whatever adjective it
     carries* — including "orphaned" and "unsupervised", and including when it ends in the
     imperative "stop them and restart one cleanly". Ambiguous ownership is not a licence to stop a
     watcher; it is a reason not to.

   **The last bullet is the common case, not a legacy edge case.** On `main` every multi-root
   verdict is a single undifferentiated list, and #3328 splits only the branch where the sentinel is
   *live* — one of three. After it lands, the no-sentinel and dead-sentinel branches still emit
   `N orphaned watcher(s) … stop them and restart one cleanly`, naming every root including the
   session-owned ones. Read structurally, that is the safe bullet; read for tone, it is the
   dangerous one. That gap is why this rule tests for two groups instead of trusting the adjective.

   **Do not re-derive this yourself.** `ps -p <pid>` prints an identical row for a supervised watcher
   and an ownerless one — the roots come from `_watcher_trees()` and are alive by construction — so a
   hand-rolled trace cannot produce the split it appears to, and eyeballing raw PPIDs is the guessing
   this step exists to prevent. That is also what the "don't hand-roll a process check" rule below
   means: the probe is the authority for ownership as well as for liveness.

   **Do NOT scope this by counting `state/cores/*.alive`.** That directory is SYNCED ACROSS HOSTS, so a
   peer's fresh heartbeat inflates the count on a single-core machine and suppresses cleanup of
   genuinely orphaned local watchers — the inverse failure, equally bad. Measured on a live host: a
   remote `Mac-186.alive` carried the *same pid* as the local record.

   | probe says | action (apply the two-group test above first) |
   |---|---|
   | `ok` | nothing to do. |
   | watcher(s) running with **no PID sentinel** (orphaned) | **Do NOT start another** — that is what creates the duplicate. This branch emits ONE undifferentiated list, so the two-group test fails: **change nothing**. Stop roots only if a future build names owned and ownerless separately here. |
   | sentinel pid dead but **other watcher(s) still run** | same — one undifferentiated list, so **change nothing**. |
   | multiple trees, some **not tracked by the sentinel**, reported as two groups | stop exactly the group with **no live owning session**; leave the session-owned group alone. If the ownerless group is empty, change nothing. |
   | not running (no sentinel, no trees) / pid dead with none running | start one with the `Monitor` tool: `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`. |

   **Never stop a watcher whose owning core is alive** — that is the invariant the table cannot
   express on its own, and the one that makes the difference between a cleanup and an outage.

   **A missing sentinel is UNKNOWN, not DEAD.** The sentinel is written once at startup (`watch-tasks-stream.sh` line ~316) and removed by cleanup only when the content still matches that pid, so an absent file cannot distinguish "no watcher" from "a live watcher whose file was removed". Measured 2026-08-07 on a live core: the watcher had held one pid for ~5h, was **functioning** (it emitted `TASK_FILE:` for a probe written during the check), and the sentinel was absent from disk entirely. The instruction this step used to carry — *missing OR dead → restart* — would have attached a second watcher to that live one, and both then emit every task, so each task gets processed twice. `health-check.py` names this failure directly at its `task-watcher` probe: restarting on a dead-looking sentinel "is what produces the duplicates in the first place."

   When notifications arrive (`TASK_FILE: <basename>`), Read the named file. Each event represents one new task — process all queued tasks before continuing.

   **Don't hand-roll a process check to second-guess the probe.** `pgrep -f watch-tasks` / `ps | grep watch-tasks-stream` both match the wrapper shell that runs the check (its own argv contains the search string), so they return a pid for a transient subshell or pick the wrapper instead of the watcher — an attempt at this on 2026-08-07 reported rc=1 with the watcher demonstrably alive. `_watcher_trees()` already solves this by scoping to process trees; use its verdict via the probe.

9.5. **⚠ BEFORE POSTING TO A PR THREAD, CHECK YOU ARE NOT TALKING TO YOURSELF (added
   2026-09-04 after measuring it on this host).** Re-verifying a standing review is right; posting
   that re-verification into silence is noise, and each repeat makes the next less likely to be
   read. Measured across 36 open PRs: **5 threads where every recent event was mine and unanswered**
   — #2300 at **7 events over 14.3 days** with the author never replying once, and I was composing
   an eighth when this check was written.

   ```bash
   python3 skills/proactive-loop/scripts/pr-monologue-check.py <number> --me <your-login>
   # 0 safe to post · 1 REFUSE, naming the run and its span · 2 could not answer (NOT a green light)
   ```

   It merges BOTH surfaces (issue comments + reviews) so a thread answered only by a review does not
   read as silence, and **drops `*[bot]` logins**: a CI bot commenting is not a human reading you.
   That bot case was a live false SAFE — `github-actions[bot]` reset a real run of 2 to 0 on #2406,
   clearing exactly the post the guard exists to stop. `--count-bots` reproduces the old answer, so
   the fix has a control rather than an assertion.

   **On a REFUSE, the answer is not "post anyway with better wording."** The thread has no reader;
   re-solicit through a stand (`collaboration-intelligence`), or leave the flag standing and spend
   the pass elsewhere. Guarded by `tests/proactive-loop-pr-monologue-check.test.py` (17 tests;
   4 mutations verified red, including one that makes a fetch failure read as safe).

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
