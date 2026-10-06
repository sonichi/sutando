# `src/agent/agy/` — agy (Antigravity CLI) launcher scaffold

Slice 1 of sonichi#4272: a minimal, standalone launcher for `agy` (Google's
Antigravity CLI, Gemini-backed), a **candidate** third Sutando core runtime
alongside Claude Code (`src/agent/claude/`) and Codex (`src/agent/codex/`).

## What this is

`cli/start-cli.sh` starts (or attaches to) a persistent tmux session running
`agy --dangerously-skip-permissions` interactively — the same persistent,
multi-turn shape as the Claude and Codex core sessions, not a one-shot `-p`
call. Before first launch it pre-seeds agy's onboarding-complete cache
(`onboarding_seed.py`) so the session lands on the same one-keypress
workspace-trust prompt Claude Code shows, instead of dead-ending on agy's
onboarding wizard. `--check` verifies `agy` is on PATH and reports auth
status without launching anything.

## What this is NOT (yet)

This scaffold is deliberately narrow — see sonichi#4272 for the full 4-slice
plan:

- **Not wired into core selection.** `src/agent/start-cli.sh` (the dispatcher
  every other launch path goes through) does not dispatch to `agy`; this
  script is only invokable directly (`bash src/agent/agy/cli/start-cli.sh`).
  `core.runtime: agy` is not a config option anywhere.
- **No task injection.** Nothing reads `tasks/*.txt` and feeds it into the
  session this starts, unlike Codex's `task-notifier.sh` or Claude's file
  bridge. A launched session is a plain interactive agy shell.
- **No scheduler/cron integration.**
- **No health-check integration, no `--restart` flag, no signal handling**
  beyond tmux's own. Not feature parity with the Claude/Codex launchers —
  see those for what a fully-integrated core runtime looks like.

A user can manually start a persistent, pre-onboarded agy session with this
script today. They cannot yet have Sutando route tasks into it, schedule work
on it, or select it as the live core.
