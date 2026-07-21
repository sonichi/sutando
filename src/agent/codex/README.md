# `src/agent/codex/` — the Codex core agent

This runtime makes the interactive Codex CLI a persistent Sutando core. The
generic dispatcher at `src/agent/start-cli.sh` selects it when
`core.runtime` is `codex`.

`cli/start-cli.sh` owns the `sutando-core` tmux session and launches Codex with
non-interactive approvals plus full local filesystem access, matching the
autonomous permissions expected by the owner-only core. `cli/task-notifier.sh`
adapts Sutando's streaming file watcher to Codex by submitting one prompt per
task-file event into the core pane. It runs in a separate managed tmux session
so it survives launcher exit and is restarted together with the core.

Codex authentication and settings are selected through the `type=codex`
entry in `core_config_dirs` (`CODEX_HOME` by default). The tracked default uses
the user's existing `~/.codex`, so switching runtimes does not copy tokens or
silently create a second login.
