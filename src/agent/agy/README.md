# `src/agent/agy/` — agy (Antigravity CLI) launcher scaffold

Slices 1–2 of sonichi#4272: a minimal, standalone launcher for `agy`
(Google's Antigravity CLI, Gemini-backed), a **candidate** third Sutando core
runtime alongside Claude Code (`src/agent/claude/`) and Codex
(`src/agent/codex/`).

## What this is

`cli/start-cli.sh` starts (or attaches to) a persistent tmux session running
`agy --dangerously-skip-permissions` interactively — the same persistent,
multi-turn shape as the Claude and Codex core sessions, not a one-shot `-p`
call. Before first launch it pre-seeds agy's onboarding-complete cache
(`onboarding_seed.py`) so the session lands on the same one-keypress
workspace-trust prompt Claude Code shows, instead of dead-ending on agy's
onboarding wizard. `--check` verifies `agy` is on PATH and reports auth
status without launching anything.

`cli/task-notifier.sh` (slice 2) gets a task from `tasks/*.txt` into that
session. agy's own async primitive (`run_command`/`manage_task`) fires on
background-**subprocess completion** only, never per-line while a process
runs — verified live — so unlike Claude Code (whose `Monitor` tool arms
`src/watch-tasks-stream.sh`, a watcher that runs forever, from inside the
agent's own turn loop), agy cannot self-arm that same never-exiting watcher.
This notifier instead runs the watcher **externally** and injects each task
into the pane via `tmux send-keys`, the same shape as Codex's
`task-notifier.sh` (Codex has no self-watch primitive at all). `start-cli.sh`
starts it, once, in its own `<session>-watcher` tmux session.

**Deliberately duplicated, not extracted** — `has_result()` and the
priority-ordered task pick in `task-notifier.sh` are the same provider-
neutral policy as Codex's `task-notifier.sh` functions of the same name.
CLAUDE.md's "Shared adapter policy" rule treats a second copy as a defect,
not a follow-up; it was kept local here only to keep this slice to its one
stated concern. Extracting both (`has_result`, `next_pending_task`'s
priority-sort wrapper, and the submit-confirm/retype state machine in
`deliver_prompt`) into a shared `src/` module both launchers call is real,
named follow-up work — not implied cleanup.

## What this is NOT (yet)

This scaffold is deliberately narrow — see sonichi#4272 for the full 4-slice
plan:

- **Not wired into core selection.** `src/agent/start-cli.sh` (the dispatcher
  every other launch path goes through) does not dispatch to `agy`; this
  script is only invokable directly (`bash src/agent/agy/cli/start-cli.sh`).
  `core.runtime: agy` is not a config option anywhere.
- **No crash-restart supervision for the notifier**, unlike Codex's dedicated
  supervisor wrapper (`task-notifier-supervisor.sh`) — a dead watcher session
  is simply recreated on the next `start-cli.sh` invocation, not resurrected
  mid-session.
- **No scheduler/cron integration.**
- **No health-check integration, no `--restart` flag, no signal handling**
  beyond tmux's own. Not feature parity with the Claude/Codex launchers —
  see those for what a fully-integrated core runtime looks like.

A user can manually start a persistent, pre-onboarded agy session with this
script today, and a task file dropped in `tasks/` will reach it and produce a
result in `results/`. They cannot yet have Sutando schedule work on it or
select it as the live core.
