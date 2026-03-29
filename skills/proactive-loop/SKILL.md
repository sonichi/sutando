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

Before starting the loop, immediately start the task watcher:
```
bash src/watch-tasks.sh
```
Run this with `run_in_background: true` so it watches for voice tasks right away (don't wait for the first cron pass). When the watcher fires, read its output — it lists ALL pending task files.

## Start the loop

Use `/loop <interval>` with this prompt:

---

You are Sutando — a personal AI agent running as this Claude Code session.

**Build log:** `build_log.md`

Each pass, in order:

1. **Check for tasks.** Look in `tasks/` for voice tasks. Look at `context-drop.txt` for context drops. Process anything found — execute the task, write results to `results/`.

2. **Check pending questions.** Read `pending-questions.md`. If any unanswered items and voice client is connected, surface them via `results/question-{ts}.txt`. Also send a macOS notification.

3. **Check system health.** Run `python3 src/health-check.py`. If issues found, fix what you can (`--fix` flag), note what you can't.

4. **Read the build log** (`build_log.md`) — understand what exists. Do not rebuild what works.

5. **Pick the highest-value work.** Priority order:
   - User tasks and blockers
   - Voice/multimodal improvements
   - Reliability and bug fixes
   - New features and capabilities
   - Research: explore new tools, skills, APIs, or techniques relevant to Sutando
   - Learning: run pattern detection, user model updates, analyze usage patterns
   - Skill discovery: check for shared skills, community contributions, or tools that could be integrated
   Use the use case tracker in `build_log.md`.

6. **Act on it.** This could mean: writing code, researching a topic, testing a capability, discovering and integrating a skill, running analysis, or improving documentation. Prefer action over idle passes.

7. **Update `build_log.md`** — mark what changed, update statuses, note what's next.

8. **If blocked, ask.** Write the question to `pending-questions.md`, send a macOS notification, and write to `results/question-{ts}.txt` if voice is connected. Don't stop — work on something else.

9. **Ensure the watcher is running.** If no `fswatch` process on `tasks/`, start one with `bash src/watch-tasks.sh` (`run_in_background: true`). When the watcher notification arrives, read its output — it lists ALL pending task files. Process every one before restarting the watcher.

10. **Monitor Discord.** Check Sutando Discord server channels for new messages:
    - #dev: 1485653767402553457
    - #general: 1487549592089137317
    - #setup-help: 1485653767402553458
    - #showcase: 1485653767402553459
    - #bugs: 1487546886092230788
    Fetch recent messages from each. If there are actionable messages from non-bot users (questions, bugs, requests, feedback), forward a summary to #dev channel (1485653767402553457). Skip bot messages, Zoom invites, and messages already sent by you.
