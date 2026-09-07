---
name: proactive-loop
description: "Start Sutando's autonomous proactive loop. Monitors tasks, runs health checks, and builds missing capabilities on a recurring schedule."
user-invocable: true
---

# Proactive Loop

One pass = the numbered steps below, in order. Each step is a command, its exit codes, and what to
do per code. The reasoning, measurements and incidents behind every step live in
[`docs/proactive-loop-rationale.md`](../../docs/proactive-loop-rationale.md) under the same step
number; read it when a step surprises you, never per pass. A rule belongs here only as a command a
mechanism enforces; prose lessons go to the rationale doc (`tests/proactive-loop-skill-budget.test.py`
caps this file and refuses date stamps in it).

**Usage**: `/proactive-loop [interval]` (default 10m).

## On activation
1. `/schedule-crons` — registers the session crons and stamps them.
2. Task watcher via the `Monitor` tool: `command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`,
   `description: 'Streaming task watcher'`. Each `TASK_FILE: <name>` line is one task to Read and process.
3. If `CronList` already shows a `main-loop` / `/proactive-loop` job, run the per-pass body directly —
   never add a second loop driver.

## Per pass
`WORKSPACE="$(bash scripts/sutando-config.sh workspace)"` once; quote every `"$WORKSPACE/..."` path.

0. **Status.** `bash scripts/core-status.sh running "<what the owner is waiting on>"`; rewrite on every
   pivot; `bash scripts/core-status.sh idle` at the end. Never `>` the JSON yourself.
0.5. **Quota tier** (Claude core):
   `python3 $CLAUDE_CONFIG_DIR/skills/quota-tracker/scripts/read-quota.py | python3 skills/proactive-loop/scripts/quota-tier.py`
   → `TIER <FULL|MEDIUM|LIGHT|MINIMAL> (bound by …)`. Then
   `python3 skills/proactive-loop/scripts/claude-quota-cadence.py --json` → if `effective_cron` differs
   from the `/proactive-loop` job in `CronList`: capture the old job id, `CronCreate` the new job with
   `prompt: "/proactive-loop"` and `cron: <effective_cron>`, confirm it in `CronList`. Then `CronDelete`
   the old id and confirm exactly one `/proactive-loop` job remains. If create or confirmation fails,
   keep the old job and stop loudly. Codex core: `python3 skills/proactive-loop/scripts/codex-quota-gate.py --json`. Tier caps
   step 6's depth (LIGHT/MINIMAL: no self-development); it never skips owner tasks, health or the log.
0.7. **Reconstruct.** Invoke the `context-reconstruct` skill (a Skill-tool call, not a mention). It
   reads `<workspace>/hosts/<host>/current-track.md` first, then the live thread
   (`python3 src/discord-read.py <channel> --serving <channel>` when serving a task, `--operator` otherwise),
   pending questions, relay, build log. Trust the record over recall; maintain `current-track.md`.
1. **Tasks.** Process every file in `$WORKSPACE/tasks/`; `access_tier: team|other` → the sandboxed path.
   Group a thread with `[deduped: task-<latest>]`, then
   `python3 skills/proactive-loop/scripts/check-dedup-targets.py "$WORKSPACE/results/<file>"`
   (0 clean · 1 the dedup delivers nothing · 2 cannot answer). All-notice groups use `[no-send]` on each.
   Marker semantics belong to `src/result_markers.py`; never re-implement them.
   Before idle: `python3 scripts/unanswered-tasks.py --workspace "$WORKSPACE"` (1 = a task got no result).
2. **Questions.** Read `<workspace>/hosts/<host>/pending-questions.md`; surface via `results/question-<ts>.txt`
   when voice is connected, plus a macOS notification.
3. **Health.** `python3 src/health-check.py`; fix with `--fix` what it can. A warn is a pointer into the
   record: before investigating, `grep -in "<entity from the warn TEXT>" "$H/pending-questions.md" "$H/current-track.md"`
   with `H="$WORKSPACE/hosts/$(bash scripts/sutando-config.sh host-label)"`; a zero means try another
   token, then `grep -n '^## ' "$H"/*.md` before concluding absence. Extend a hit; never re-file it.
3.4. **Zero-result rule.** Before reporting any empty result or telling the owner how the system behaves:
   `python3 skills/proactive-loop/scripts/warn-already-triaged.py --claim "<the sentence>"`
   (1 already parked → read and extend · 0 untriaged · 2 cannot answer, not a green light). Name what a
   non-zero would look like, run the positive control, suspect scope then shape, never verify in zsh.
3.45. **Duplicate issue gate**, chained so a refusal cannot be skipped:
   `python3 skills/proactive-loop/scripts/gh-duplicate-check.py --repo <owner/name> --title "<title>" && gh issue create --repo <owner/name> --title "..." --body-file <f>`
   (0 no candidate · 1 do not file, candidates named · 2 cannot answer).
3.5. **Policy.** `python3 skills/proactive-loop/scripts/self-development-enabled.py` → `disabled` skips
   4–8, 10, 11. Owner-requested tasks, pending questions, health/service recovery and the watcher remain
   active. Manual `/proactive-loop` invocation does not override the policy.
3.6. **Tool suites.** `python3 skills/proactive-loop/scripts/tool-suites-check.py --workspace "$WORKSPACE" --repo "$PWD"`
   (0 pass · 1 a suite failed · 2 cannot answer). Extra suites are declared in
   `$WORKSPACE/hosts/<host>/tool-suites-extra.json`. It passes no argv to a suite.
4. **Build log.** Read `$WORKSPACE/build_log.md`; do not rebuild what works.
5. **Pick** the highest-ROI unblocked item: owner tasks and blockers, then peer `opinion-requested` /
   `review-requested` claims in #bot2bot, then voice reliability, then regressions, then the menu.
   Write the pick into the status `step`.
6. **Act.** A blocked primary is a cue to switch lanes, never to idle. Before pivoting from the owner's
   latest ask, read `state/last-owner-activity.json` and announce the pivot in the bot-to-bot channel
   per `PERSONAL_CLAUDE.md`. Skip this step only for: MINIMAL tier; owner active in the last ~5 min;
   `state/presenter-mode.sentinel` (`bash scripts/presenter-mode.sh`); `state/loop-paused-until.sentinel`;
   `python3 src/shutdown.py check` exiting 0 (finish in hand, write idle, do not relaunch).
6.5. **Idle surface.** Record the pass:
   `python3 skills/proactive-loop/scripts/idle-surface-hash.py --state "$WORKSPACE/state/idle-streak.json" --pass-outcome substantive|noop`.
   The held set is edited only through
   `python3 skills/proactive-loop/scripts/idle-held.py --state "$WORKSPACE/state/idle-streak.json" --remove <id> --reason "<why>" --add <id>:<gate>`
   (no whole-list interface; a removal needs a reason); audit notes with `--audit-notes "$PWD"` and
   retire merged items. Compute: `idle-held.py … | idle-surface-hash.py --state …` → `post <hash>` or
   `quiet <hash>`. On `post`: send ONE FYI line to the owner's primary channel, THEN re-run with
   `--write` and `--commit`. Never commit before the send; never build the list from recall.
   `quiet` + owner active in the last ~30 min → still drop a one-line activity signal.
6.7. **Failure closure.** Every reported failure ends with the mechanism that prevents its recurrence,
   linked, or the sentence "no mechanism exists, because X". A filed lesson is not a third option.
7. **Build log write.** Append with `O_APPEND` and a random marker; assert `count(MARK) == 1` by reading
   the file back. Never read-modify-replace. Then decide whether a `relay/relay-<ts>.md` note is owed
   (a PR event, a resolved question, a lifted or new blocker, a judgment) — most passes owe none.
7.5. **Memory index.** Before adding a row to `MEMORY.md`:
   `python3 skills/proactive-loop/scripts/memory-index-budget.py --adding "<row>"` (0 safe · 1 refuse,
   casualty named · 2 cannot answer). On refusal free room FIRST and check the row is still reachable
   from its hub before removing it; which rows go is the owner's call.
8. **Ask.** Insert the question ABOVE the `# Resolved` divider of the per-host `pending-questions.md`,
   placed by importance (only the top 5 render anywhere), and assert with the reader:
   `python3 -c "…src/check-pending-questions.py…get_waiting_questions()"` — count went up, title matches,
   position ≤ `VISIBLE_PREFIX`. macOS notification; `results/question-<ts>.txt` when voice is connected.
   Then pivot; never block.
9. **Watcher.** Act only on the `task-watcher` probe from step 3. Stop pids only when the probe presents
   owned and ownerless as two separately labelled groups; one undifferentiated list means change nothing.
   Not running with no trees → `Monitor` `bash src/watch-tasks-stream.sh` persistent. A missing sentinel
   is UNKNOWN, not dead; never hand-roll a process check.
9.5. **PR thread gate**, before posting to a PR thread:
   `python3 skills/proactive-loop/scripts/pr-monologue-check.py <number> --me <your-login>`
   (0 safe · 1 refuse, run and span named · 2 cannot answer). On refuse, re-solicit through a stand.
10. **Discord.** Check the channels in `reference_discord_channels.md`; forward actionable public items to
    the dev channel. #bot2bot tags: `claim:` `blocked:` `done:` `ping:` `nack:` `opinion-requested:`.
    First PR opened wins a claim. Bots never merge. Three unresolved round-trips → both positions to
    `pending-questions.md`, proceed with the cheaper-to-reverse option.
11. **Heartbeat.** Substantive pass + #bot2bot configured + other bot active → `done: <one line>` via the
    `bot2bot-post` skill. Never fall back to `results/proactive-*.txt`. Never write `contextual-chips.json`.
