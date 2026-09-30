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

## Externally managed monitor and heartbeat

An embedder that owns both helpers can opt out of the launcher's helper lifecycle:

```bash
bash src/agent/start-cli.sh --runtime codex --external-helpers "$RECEIPTS"
```

`$RECEIPTS` must be an existing owner-private directory below the resolved
workspace's `state/`. Start the real helpers from the selected checkout with
absolute script paths, the same resolved workspace and the selected
`SUTANDO_TMUX_SOCKET` / `SUTANDO_TMUX_SESSION`:

```bash
python3 "$REPO/src/core-input-watch.py" --socket "$SUTANDO_TMUX_SOCKET" \
  --session "$SUTANDO_TMUX_SESSION" --out "$WORKSPACE/state/core-supervisor.json" \
  --no-auto-answer --no-chat-escalation --helper-receipt-dir "$RECEIPTS"
python3 "$REPO/src/core_heartbeat.py" --helper-receipt-dir "$RECEIPTS"
```

The embedder starts these as its own processes with private log destinations.
Each helper atomically writes its own `monitor.json` or `heartbeat.json` receipt
(mode 0600), naming its actual PID/start identity and resolved configuration.
One-shot or active-monitor modes refuse receipt publication. The launcher checks
receipt ownership, live kernel argv/start identity, checkout, workspace, socket,
session and passive policy before changing the core and again after startup.
The initial process identities are pinned across these checks; a different valid
helper cannot silently replace one during launch.
Missing, stale, unreadable or mismatched helpers fail the launch; there is no
helper spawn, replacement, log redirect or heartbeat stop in this mode, including
`--restart`. Other runtimes reject this option. Without it, behavior is unchanged.

Receipts are startup identity evidence, not core readiness or a security boundary
against the same OS user. The embedder must independently observe actual core
readiness and keep supervising both helper processes. A post-start failure can
leave the new core running; its owner must stop it. This option does not disable
schedulers, earned-reset timers, authentication checks or the task notifier, and
does not isolate the Codex application home. It must not be treated as a general
sandbox or as permission to fabricate helper state.

### Leave schedule provisioning to the caller

An embedder can independently pass `--no-schedule-reconcile` to skip startup's
durable-cron reconciliation, Codex scheduler installation and earned-reset timer
installation for that invocation:

```bash
bash src/agent/start-cli.sh --runtime codex --external-helpers "$RECEIPTS" \
  --no-schedule-reconcile
```

This does not stop, disable or rewrite existing jobs, change their configuration,
or suppress tasks they already deliver. The caller owns schedule provisioning;
pending schedules are not installed by this launch. Without the flag, all three
startup reconciliation paths run as before. The flag is Codex-only, takes no value
and is not persisted: pass it again on each invocation, including `--restart`.
Authentication, helper checks, the notifier and core startup remain active.
The flag does not isolate the Codex home or change helper ownership by itself.

## Automatic earned resets

On macOS, launching a Codex core or Codex worker installs a five-minute
LaunchAgent for its configured `CODEX_HOME`. It reads the live Codex App Server
quota and automatically redeems one available earned reset only when the Codex
weekly window reports at least 99.9% used and its next reset is at least 24
hours away. One timer serves sessions that share a Codex home. The job runs
without an active agent session and checks the account again before spending
a credit. API-key-only accounts and accounts without earned reset credits are
left alone.

The Codex CLI must return `workspaceRouting.chatgptAccountId` from
`account/read` so Sutando can tie redemption state to the authenticated
account. This works with Codex CLI 0.157.0; 0.154.0 does not return that field.
When it is missing, the timer reports `unsupported-codex-cli` and skips
redemption without spending a credit. The timer keeps the stable Codex and
Python executable paths, so CLI and package-manager updates can replace their
symlink targets without waiting for another core or worker launch.

The CLI currently reports `usedPercent` as a whole number. In practice the
99.9% rule fires when it reports **100% used**; Sutando cannot detect exactly
0.1% remaining until the API returns finer precision. A successful redemption
is followed by a fresh quota read, and the timer keeps the same idempotency key
when retrying an uncertain request.

The redemption state is shared by Codex cores and workers in one Sutando
workspace, including sessions with different Codex homes. Separate Sutando
workspaces logged into the same ChatGPT account do not share that state; run
automatic redemption from only one of those workspaces.

This feature is enabled by the proactive-loop skill's
`SUTANDO_CODEX_AUTO_RESET_ENABLED=1` manifest setting. Set
`SUTANDO_CODEX_AUTO_RESET_ENABLED=0` in the launcher's environment to disable
redemption; restart the Codex core or worker so the timer captures the change.
An installed timer then wakes but exits without requesting a reset. An unset
value preserves a prior explicit disable; set `SUTANDO_CODEX_AUTO_RESET_ENABLED=1`
and restart to re-enable it. Check the timer
with:

```bash
python3 skills/proactive-loop/scripts/codex-auto-reset-timer.py status \
  --workspace "$(bash scripts/sutando-config.sh workspace)" \
  --codex-home "$(bash scripts/sutando-config.sh core-config-dir-value codex)"
```

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
