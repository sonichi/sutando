# Changelog

All notable changes are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [v0.1.0] — 2026-05-28

First tagged release. Engine is stable for single-machine installs; multi-machine sync and migration framework ship in v0.2.0.

> This release has no automated migrations. Future releases will ship a `migrations/` runner.

### Added

**Infrastructure**
- Workspace contract (`~/.sutando/workspace/`): all runtime state (tasks, results, state, logs, notes) lives under a single workspace directory, separate from the repo checkout and Sutando.app bundle. Helpers `resolve_workspace()` / `resolveWorkspace()` provide the canonical path; `status_path()` / `statusReadPath()` for state files ([#821], [#837], [#940])
- Migration runner scaffold: `src/run_migrations.py` + numbered `migrations/` registry with idempotent runner and `schema-version.json` ([#1295])
- Health check: `src/health-check.py` monitors all bridges, services, disk state, and workspace invariants with `--fix` auto-repair ([many PRs])
- Startup skill: single-entry bootstrap that starts all bridges, watcher, screen capture, and credential proxy ([#1072])
- Task orphan recovery: stranded task files from previous sessions are re-queued on startup ([#1074])
- Core heartbeat: `src/core_heartbeat.py` writes a per-host `.alive` file every 30s for multi-core lease coordination ([#1295-adjacent])

**Messaging bridges**
- Telegram bridge: TOFU onboarding, access tiers (owner/team/other), file attachments, proactive DMs ([many PRs])
- Discord bridge: DM + channel @mention routing, access tiers, file attachments ([#1077], [#1078], [#1148])
- Slack bridge: Socket Mode, access tiers, TOFU, outbox log ([many PRs])
- Phone conversation: Twilio WS audio + task bridge for inbound/outbound calls ([many PRs])

**Task pipeline**
- Multi-source task routing: voice, Discord, Telegram, phone, chat-path, context-drop — unified `tasks/` file bridge with access-tier enforcement ([many PRs])
- Result delivery: per-channel delivery, `[file:]` attachments, `[channel:]` redirect, `[deduped:]` thread consolidation, `[no-send]` / `[REPLIED]` markers ([#1029], [#1033])
- Outbox log: `src/outbox_log.py` records all outbound bridge sends for auditability ([#931])
- Single-instance guard: `fcntl.flock` prevents duplicate bridge processes on restart ([#1257])

**Conversation store**
- SQLite mirror of voice conversation log, per-surface tables, session rollup queries ([#791], [#1051])
- Dedup migration + idempotent import for conversation history ([#941])

**Reliability**
- `watch-tasks-stream.sh`: persistent inotifywait-based task watcher with PPID orphan guard and EPIPE buffer fix ([#1063], [#1088])
- Single-instance bridge guard via `fcntl.flock` ([#1257])
- Launchd plists: Sutando.app crash-restart supervisor + credential-proxy KeepAlive ([#942], [#1086])

**Quota tracker**
- `skills/quota-tracker/`: tracks Claude Code usage, burn-rate EWMA, passes-left forecast, proactive degradation tiers ([#1087])

**Developer experience**
- `cwd-lint` CI: bans bare `process.cwd()` / `Path.cwd()` outside canonical resolvers ([#863])
- Host-CLI dependency snapshot: prevents new accidental `~/.claude/` hard-codings ([#864])
- PEP-604 union annotation lint: catches Python 3.9 incompatibilities at CI time ([#961])
- `open-sutando-ref` skill: fuzzy GitHub-ref resolver — `#874`, `PR 874`, `issue #874`, free-text ([#903])

### Fixed

- Context-drop tasks (Sutando.app hotkey) now archive correctly when no bridge consumer is present ([#969])
- `check-pending-questions.py`: free-form sections without a `**Status:**` marker are now treated as unanswered ([#1326])
- Watch-tasks: EPIPE-buffer + PPID=1 orphan leaks closed ([#1088])
- Proactive `[channel:]` redirect for loud-failure when target channel is unreachable ([#1147 follow-up])

### Changed

- `status_read_path()` / `statusReadPath()`: legacy workspace-root fallback removed (one-release shim, now safe to drop) ([#943], [#945])

[v0.1.0]: https://github.com/sonichi/sutando/releases/tag/v0.1.0
