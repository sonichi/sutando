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

Watcher events are wake signals rather than queue order. The notifier waits for
the core's positive `idle` status and a pane without Codex's live working marker
before touching the interactive input, then selects the highest-priority pending
task from disk (`urgent`, `normal`, `low`, FIFO within a tier). This keeps a busy
core from losing an injected prompt and prevents scheduled low-priority work
from blocking later owner messages. It also re-scans the durable queue every 30
seconds, so a missed filesystem event can delay a task but cannot strand it.

On macOS the launcher also reconciles fixed `crons.json` schedules onto the
OS-backed cron runner before starting or reusing the Codex session. Codex has
no session `CronCreate` surface, so leaving those entries session-owned would
make them silently stop after a runtime switch or restart. Separately, the
launcher reconciles the durable Codex scheduler: while this runtime is
selected, the canonical `main-loop` entry is converted at read time into one
silent proactive-pass task per cron fire; the user's `crons.json` remains
unchanged for runtime switching.

Codex authentication and settings are selected through the `type=codex`
entry in `core_config_dirs` (`CODEX_HOME` by default). The tracked default uses
the user's existing `~/.codex`, so switching runtimes does not copy tokens or
silently create a second login.
