# Codex CLI as the Sutando core

Sutando can use either Claude Code or Codex CLI as its persistent task
execution core. Claude remains the upgrade-safe default. To select Codex, add
this gitignored per-clone override:

```json
{
  "core": { "runtime": "codex" }
}
```

Save it as `sutando.config.local.json`, then run:

> **Never run `--restart` from inside the sutando-core session** — it kills the canonical
> session, which is the agent running the command. Run it from a terminal outside the core,
> or have the owner type `restart core` in a **Discord** DM — that is the only adapter wired
> to it (`src/discord-bridge.py:3032`; slack and telegram do not import
> `core_restart_intent`), and it needs Sutando.app running to consume the intent.
>
> The intent *policy* itself lives in two places, not one: `src/core_restart_intent.py` and a
> hand-written Swift mirror in `src/Sutando/main.swift` (`:427`, `:2661` — "mirror
> core_restart_intent.py exactly": consume-before-act, 10-minute staleness drop). They are kept
> in sync by comment, so a change to one is a change owed to the other.
>
> This is a rule to keep, not a guard that
> keeps it: the Codex launcher exports `SUTANDO_CORE_SESSION=1` unconditionally
> (`src/agent/codex/cli/start-cli.sh:39`) and its `--restart` branch (`:242-244`) does no
> inherited-marker check, so the call is not refused on this path.

```bash
codex login status
bash src/agent/start-cli.sh --restart
```

The tracked `type=codex` config entry sets `CODEX_HOME=~/.codex`, deliberately
reusing the user's authenticated Codex installation rather than copying login
tokens into the Sutando workspace. A local config may replace
`core_config_dirs` to select another home; because arrays replace wholesale,
include every Claude/Codex entry you still need.

## Runtime behavior

`src/agent/start-cli.sh` is the only generic launch/restart entry point. It
resolves `core.runtime` and delegates to the matching implementation.

The Codex implementation:

- is selected before startup touches Claude credentials, so `startup.sh` does
  not copy Claude login state or run the Claude-only preflight for a Codex core;
- exports the configured `CODEX_HOME` and requires `codex login status` to pass
  before any background service launches;
- owns the same `sutando-core` tmux session used by the menu bar, health checks,
  and terminal attachment;
- validates `codex` availability and authentication before changing the live
  session;
- uses approval policy `never` and sandbox `danger-full-access`, matching the
  full local access required by the owner core;
- enables web search and makes the user's home directory available;
- honors `SUTANDO_CORE_MODEL`, `SUTANDO_CORE_WORKING_DIR`, and the more specific
  `SUTANDO_CODEX_WORKING_DIR`;
- runs a separate managed `sutando-core-watcher` tmux session that converts
  task-file events into queued Codex prompts, including exact task and result
  paths;
- runs the shared core supervisor so dashboard/runtime health signals continue
  to update when Codex is selected;
- restarts the core and notifier together, preventing duplicate task consumers.

`SUTANDO_SKIP_AUTH_PREFLIGHT=1` bypasses either runtime's early authentication
check for one startup. The runtime launcher still performs its own defensive
authentication check before replacing the core session.

For a one-command trial without changing config, use the invocation-scoped
override:

```bash
SUTANDO_CORE_RUNTIME=codex bash src/agent/start-cli.sh --restart
```

## Rollback

Set `core.runtime` back to `claude` (or remove the local override) and run:

```bash
bash src/agent/start-cli.sh --restart
```

Task and result files are runtime-neutral, so queued work survives the switch.

## Diagnostics

```bash
bash scripts/sutando-config.sh core-runtime
bash scripts/sutando-config.sh core-config-dir-value codex
codex login status
tmux -S /tmp/sutando-tmux.sock attach -t sutando-core
tmux -S /tmp/sutando-tmux.sock capture-pane -p -t sutando-core
```

If tmux is unavailable, the launcher can still open Codex directly, but
automatic file-bridge wakeups are disabled; install tmux for unattended use.
