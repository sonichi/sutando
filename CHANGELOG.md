# Changelog

All notable changes are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [v0.3.0] — 2026-06-05

> **DRAFT — release-prep PR.** Polish and curate before tagging. Items marked `<!-- review -->` warrant a closer look; entries grouped by theme. The headline change is the workspace-contract rollup (M0 + M1 + M2 + sync-workspace) — that subsection is dedicated below.

### Added

**Workspace contract rollup (M0 + M1 + M2 + sync-workspace)** — the staging-workspace-revamp work
- **M0 — in-repo workspace default:** the workspace now defaults to `<repo>/workspace/` — **supersedes the v0.1.0 `~/.sutando/workspace/` default.** Configuration moves to `sutando.config.local.json` (per-clone, gitignored) with a clear precedence: config file > baked-in default. **`$SUTANDO_WORKSPACE` is no longer honored** by the resolver as of PR #1440 — setting it triggers a one-time stderr deprecation warning + bootstrap auto-migration in `src/startup.sh`. Existing users on the v0.1.0 path are auto-migrated on next startup; manual relocation via `bash scripts/sutando-migrate.sh` is recommended for clean state. ([#1395], [#1397], [#1399], [#1440])
- **M1 — workspace migration script + `/sutando-migrate` skill:** `bash scripts/sutando-migrate.sh` audits state across legacy sources (A: repo-root, B: `~/.sutando/workspace/`, C: `$SUTANDO_WORKSPACE`), surfaces collisions with newest-mtime resolution + keep-both sidecars, gets owner confirmation, commits, verifies, and (optionally) deletes the source after a 30-day grace window. Per-file atomic (`cp -p` to sibling tmp → `mv`) with sha256 verification. Pre-flight scan + per-file progress reporter for visibility on large migrations. ([#1403], [#1406], [#1440], [#1473])
- **M2 — `claude_sutando_config_dir`:** a per-workspace shell that holds Claude Code state (`projects/`, `skills/`, `agents/`, `commands/`, `settings.json`). `--migrate`/`--import` engine copies-and-warns, with post-copy path rewriting and weight-reduction excludes. ([#1415], [#1424], [#1429]). **Schema evolution in this release:** the single-purpose `claude_sutando_config_dir.subdir` field is superseded by the new `core_config_dirs` schema (see new bullet below); legacy field is honored for one release with a hard-fail when both are set. ([#1470])
- **`core_config_dirs` per-runtime env-override schema for `sutando.config.json`:** list-of-`{id, type, env_name, synced, value}` extensible surface that replaces single-purpose `claude_sutando_config_dir.subdir`; codex/gemini drop in cleanly as additional entries with no schema bump. `synced=true` invariant actively validated. `${WORKSPACE_DIR}` token mirrors `${REPO_DIR}` for consistent expansion. Hard-fail (`ValueError`) when both new + legacy fields are set simultaneously. ([#1470])
- **Workspace-as-git-repo sync (`sync-workspace.sh`):** the workspace IS the git repo; per-host branch identity (`host/<host>/<wsId>`), config-driven gitignore carrier (`vault.sync.include` / `vault.sync.exclude`), vault URL from config (or `--vault-url` flag), handles unrelated-histories on first cross-host pull, moves sync rules to `.git/info/exclude` to avoid workspace-gitignore leak, migrates pre-wsId flat branches. ([#1445], [#1446], [#1447], [#1458], [#1459], [#1460], [#1461], [#1463])
- **Sync-memory consolidation:** legacy `scripts/sync-memory.sh` anchored to `$SCRIPT_PARENT` (not `$REPO_DIR`); emits deprecation banner in favor of `sync-workspace.sh`. ([#1432])
- **Catchup-after-startup skill** (`/catchup-after-startup`): reconstructs prior-session context — open PRs, in-flight tasks, sqlite voice/phone/discord rollups, build_log tail, health probe — into the new session's conversation buffer before the user types. PID-stamp sentinel survives unclean exits. ([#1431])
- **Relay skill** (`/relay`): write a human-authored handoff note for the NEXT session capturing intent + judgment the structured snapshot can't carry. Event-triggered, append-on-thread, archived on consumption. ([#1430])

**Voice / multimodal**
- Voice work-tool both-approaches: confirm-on-misheard + attached recent transcript so delegated tasks include the user's exact words. ([#1342])
- Unified base-mode resolver for the voice agent (closes the mode-state confusion that surfaced in #1410 / #1412 / #1413). ([#1434])
- Discord-voice: meeting mode + per-speaker access tiers + "za-warudo" magic-word activation. ([#1311])
- Screen-companion: implements `vision_query`, `take_note`, `look_up_reference` inline tools (re-applied after revert; original feature from #819). ([#1362])

**Bridges**
- Discord bridge: emit `parent_message_id` for replies so multi-turn DM threads thread correctly client-side. ([#1346])
- Discord bridge: route all marker handling through a single `parse_markers()` parser (refactor for [#896]). ([#1302])
- Slack bridge: recover from external `access.json` deletion via in-memory cache (closes [#899]). ([#1292])
- Slack bridge: recover orphan `.sending` files on startup. ([#1290])
- Telegram bridge: deliver briefing / insight / friction results to owner DM. ([#1350])
- Single-instance bridge guard via `fcntl.flock` (closes [#1257]). ([#1317])
- Mass-deletion tripwire on `sync-memory.sh` before push. ([#1349])

**Skills**
- `email-find` skill for stubborn email lookups when targeted searches return nothing. ([#1020])
- `obsidian-vault` skill: opt-in agent vault — capture skill + state mirror + Opus-judged nightly dream. ([#1082])
- `open-sutando-ref` fuzzy GitHub-ref resolver: `#874`, `PR 874`, `issue #874`, or free-text → URL. ([#1308])
- `morning-briefing` skill works without gws dependency. ([#1282])
- `quota-tracker` skill: burn-rate EWMA + passes-left forecast (closes [#1087]). ([#1319])

**Reliability**
- Sutando.app launchd KeepAlive supervisor — crash-restart + login auto-start (closes [#942]). ([#1294])
- Self-heal for the 1M usage-credit gate wedge + fix watchdog silent no-op. ([#1428])
- Schedule-crons: no inline-fire on registration + per-cron queue gate. ([#1437])
- Bridges: inject skill instructions into owner task files so they survive context compaction across long sessions. ([#1467])
- Archive stranded `.claimed-core-N.txt` task files (closes [#933]). ([#1299])
- Voice-agent: clear stale pid file before launchctl kickstart. ([#1400])
- Loud-warn when workspace fallback masks a `.env` override. ([#1369])

**Developer experience / CI**
- `cwd-lint`: bans bare `process.cwd()` / `Path.cwd()` outside canonical resolvers (closes [#863]). ([#1322])
- Host-CLI dependency snapshot + migrate hardcoded `~/.claude/` paths (closes [#864]). ([#1324])
- PEP-604 union annotation lint catches Python 3.9 incompatibilities at CI time (closes [#961]). ([#1305])
- CLA-recheck workflow dedups `@cla-assistant check` comments. ([#1353])
- Catchup-after-startup: migrate stale `SessionStop` → `SessionEnd` in `settings.json`. ([#1374])

### Fixed

**Migration — Lucy's Maddy v0.8 5-bug sweep (2026-06-06)**
- `sutando-migrate`: auto-invoke `--import` to copy Claude memory; slug-rename bridge for `<repo-slug>` → `<repo-slug>-workspace` variant when post-M0 read-slug differs from source-slug. ([#1475])
- `startup.sh`: honor `sutando-migrate` per-source sentinels (`state/.migrated-from-<tag>-<id>`); suppress the re-migrate-every-boot loop when `$SUTANDO_WORKSPACE` is still set in shell rc after a manual migration. ([#1478])
- `sutando-migrate`: `backup_dest` default media excludes (`*.mp4 *.mov *.mkv *.avi *.webm`, archives, `notes/asset-library/`, `node_modules`, `.git`) + skip gzip when surface payload exceeds 5GB (configurable via `SUTANDO_MIGRATE_BACKUP_GZIP_THRESHOLD_MB`). On a 34GB media-heavy workspace: 30+ min stall → ~5s. ([#1482])
- `sync-workspace`: refuse push/pull when `.git` is present but sync was never initialized — two-tier guard accepts either `.git/info/exclude` marker or `.sutando-vault/ws-id` as proof of init; bypass with `SUTANDO_SYNC_SKIP_INIT_GUARD=1`. ([#1483])
- `startup.sh`: tee stdout + stderr to `/tmp/sutando-startup-<UTC-ts>-<PID>.log` at script top so the trace survives operator redirects into a dir the migration then `rm -rf`'s. ([#1484])

**Cross-platform**
- `sutando-migrate`: `uname -s` branch for preflight byte count — BSD `stat -f '%z'` (macOS) vs GNU `stat -c '%s'` (Linux). Closes the Linux portability gap surfaced in #1474. ([#1476])

**Security — `execSync` → `execFileSync` sweep (#1451)**
- `meeting-tools.ts`, `task-bridge.ts`, `recording-tools.ts`: convert `execSync` → `execFileSync` throughout to eliminate shell-injection surface. ([#1452], [#1462], [#1466])

- Voice-agent restart cleanly clears stale pid file before kickstart. ([#1400])
- Bridges + services: narrow bare `except:` → `except Exception:` across 5 Python services. ([#1398])
- Dashboard pending-count reads free-form sections (not just `**Status:**` markers). ([#1405])
- Pending-questions parsers honor the `# Resolved` divider. ([#1402])
- `claude-gemini` skill: guard empty `INCLUDE_DIRS` under `set -u` on bash 3.2. ([#1391])
- Defer annotations + `timezone.utc` in `quota-tracker` & `deal-finder` skills (Python 3.9 compat). ([#1385])
- `slide_control` inline-tool description matches implementation. ([#1396])
- `type_text` inline-tool mode param + in-place-edit routing. ([#1394])
- `screen-companion`: `work` (task delegation) reachable in all modes (closes [#1375]). ([#1365])
- `call-diagnostics`: schema-drift on sessions SELECT + phone timeline accuracy (closes [#1357]). ([#1363])
- `phone-conversation`: archive task + result files instead of unlinking (closes [#1235]). ([#1237])
- `agent-registry`: grep `.env` for `SUTANDO_WORKSPACE` when env var is unset. ([#1368])
- `screen-companion`: remove `SPEECH REQUIREMENT` block (revert prompt fix). ([#1373])
- `startup`: source `.env` before `init.sh` so workspace overrides are honored. ([#1367])
- `catchup-after-startup`: rename `SessionStop` → `SessionEnd` hook. ([#1366])
- `screen-companion`: `deactivate_screen_companion` missing `async`. ([#1358])

### Changed

- `sync-memory.sh` is **deprecated and will be REMOVED in v0.4.0** — switch to `sync-workspace.sh` now. Migrate by: running `bash scripts/sync-workspace.sh --init` once per machine, moving the vault URL from `.env` to `sutando.config.local.json` under `vault.remote_url` (or passing `--vault-url`), and replacing the cron entry. ([#1446], [#1472])
- `community-use-cases/` moved to `docs/`; removed root `/logs` and `/data`. ([#1380])
- Docs: README section header rename + remove WIRE-specific opening. ([#1382])
- Docs: `email-find` skill addresses [@qingyun-wu]'s 4 review points from [#1020]. ([#1352])
- Docs: v0.3.0 docs sweep — replaced `docs/memory-sync.md` with `docs/workspace-sync.md` (canonical doc for the new sync flow + migration risk checklist + Keychain-bound gh-token troubleshooting); promoted `docs/release-process-consolidated.md` (RFC) → `docs/release-process.md` (canonical, ratified) and deleted the two proposal docs; rewrote `docs/workspace-design.md` around the 2-space model (Code + Workspace, with Memory as a sub-path) per owner directive 2026-06-04; updated `docs/workspace-contract.md` status to "Ratified as of PR #1440 merge"; updated `docs/workspace-config.md` resolution order to drop the no-longer-honored `$SUTANDO_WORKSPACE` env var. ([#1472])

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

[v0.3.0]: https://github.com/sonichi/sutando/releases/tag/v0.3.0
[v0.1.0]: https://github.com/sonichi/sutando/releases/tag/v0.1.0
