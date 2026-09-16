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

`cli/task-notifier.sh` (slice 2) gets a task from a dedicated `tasks-agy/*.txt`
inbox into that session (see "Task inbox" below for why it is not the
canonical `tasks/`). agy's own async primitive (`run_command`/`manage_task`)
fires on background-**subprocess completion** only, never per-line while a
process runs — verified live — so unlike Claude Code (whose `Monitor` tool
arms `src/watch-tasks-stream.sh`, a watcher that runs forever, from inside the
agent's own turn loop), agy cannot self-arm that same never-exiting watcher.
This notifier instead runs the watcher **externally** and injects each task
into the pane via `tmux send-keys`, the same shape as Codex's
`task-notifier.sh` (Codex has no self-watch primitive at all). `start-cli.sh`
starts it, once, in its own `<session>-watcher` tmux session — after
confirming `fswatch` (the notifier's hard dependency) is present, and after
verifying the watcher session survives a couple of seconds rather than
trusting `tmux new-session`'s exit code alone.

**Completion-detection and priority-selection are shared, not duplicated** —
`src/delivery/task_dispatch.py` is the one place both this notifier and
Codex's `task-notifier.sh` answer "does this task have a result yet?" and
"which pending task is next?" (CLAUDE.md's "Shared adapter policy" rule: two
adapters interpreting the same workspace state get a dependency-light `src/`
module, not a second bash copy — the copy that used to live here diverged
from correct: it read a zero-byte partial result as delivered, and matched
another task's archive by a leading-digit glob instead of an all-digits
epoch suffix). The submit-confirm/retype state machine in `deliver_prompt`
remains agy-specific — it drives agy's own TUI markers, not shared policy.

## Task inbox

agy is **not** wired into core selection (see below), so its watcher session
runs ALONGSIDE whichever core (Claude or Codex) actually owns this host, not
instead of it. To keep that safe without needing real exclusive-core-selection
(out of scope for this scaffold), the notifier defaults to a separately-owned
inbox — `<workspace>/tasks-agy/` and `<workspace>/results-agy/` — rather than
the canonical `tasks/`/`results/` the live core's own watcher polls. Nothing
writes into `tasks-agy/` automatically today; a task reaches agy only when
something is pointed at that directory explicitly (or `SUTANDO_TASKS_DIR` /
`SUTANDO_RESULTS_DIR` are overridden). Wiring a real producer into that inbox,
or real exclusive-core-selection, is future work.

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
script today, and a task file dropped in `tasks-agy/` will reach it and
produce a result in `results-agy/`. They cannot yet have Sutando schedule
work on it or select it as the live core.
