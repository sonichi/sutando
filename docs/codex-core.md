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
bash skills/install.sh
bash src/agent/start-cli.sh --restart
```

With `core.runtime: codex`, `skills/install.sh` links every repo skill and every
`<workspace>/skills/` skill into `$CODEX_HOME/skills/` (resolved by
`scripts/sutando-config.sh codex-home-path skills`) as well as Claude's skills
directory. Codex's bundled `.system/` skills and any entry already there with the
same name are left alone. Codex reads skills at session start, so restart the core
after installing.

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
- forwards `CODEX_HOME` and `CLAUDE_CONFIG_DIR` into the core session, so skills
  that read the engine's channel state (such as `ag2space/access.json`) find it;
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
- runs a separate `sutando-core-observer` tmux session (`cli/codex-observer.mjs`)
  that follows the core's rollout file and writes its HealthStatus runtime
  observation record (`docs/health-snapshot.md`, "Runtime observation");
- restarts the core and notifier together, preventing duplicate task consumers.

## Task delivery: send-keys or queue

By default the notifier types each task into the core's pane with `tmux send-keys`.
With Codex 0.157 or later you can opt in to `codex queue`, which hands the task to
the TUI's shared app-server daemon; the TUI runs it at its next idle point, so no
keystroke can be lost or merged with a draft:

```json
{
  "core": { "runtime": "codex", "codex": { "delivery": "queue" } }
}
```

Restart the core (`bash src/agent/start-cli.sh --restart`, from outside the core)
for a change to take effect. Queue mode applies to the core's notifier only; a pool
worker's notifier always uses send-keys.

Only one core notifier runs per workspace. The notifier re-executes itself under
`src/delivery/notifier_lock.py`, which holds an exclusive `flock` on the workspace's
`state/codex-notifier.lock` on fd 9. The notifier and every child it starts inherit that fd, so
the lock is held until the last of them exits, `kill -9` included, and the kernel then drops it;
no lock is ever left behind. `codex_queue.py` passes fd 9 on to the `codex queue` it starts. A
child that outlives a killed notifier (a `tmux` call, a Python helper, a `codex queue` send) could
still deliver, so the next notifier waits for it on purpose:
the lock is the fence, with no pid records and nothing killed. Those children are short-lived and
bounded by their own timeouts. A wait is logged, and `health-check.py` reports one that lasts.
At startup the notifier confirms that the inherited fd really holds this workspace's lock before
acting as its owner; a stale or copied `SUTANDO_NOTIFIER_LOCK_FD` makes it wait for the lock instead.
Never delete or replace `state/codex-notifier.lock`: a holder keeps the old file, so a new file at the
same path would admit a second notifier. Nothing in the repo removes it, and a waiter that finds the
path replaced while it waited reopens the new file rather than trusting the old one.

The trade-off is deliberate: a child that hangs while holding fd 9 stalls delivery until it exits,
because a hung child cannot be told from a slow one that is about to type. The waiting notifier
logs every 30 s, health-check warns at 180 s, and a managed notifier exits 75 at 600 s and is retried
by its supervisor while the warning stays up. This rests on two invariants: the watcher is started
with `9>&-`, and no long-lived background job the notifier starts may inherit fd 9.
The lock is kept out of the long-lived watcher the notifier starts. That watcher leads its
own session, so it outlives a `kill -9` of its notifier and would keep the inbox to itself.
The notifier records the watcher's pid, start time and inbox in
`state/codex-notifier.watcher`, and the next notifier, holding the lock, stops that exact
watcher before starting its own. Pool workers have their own inboxes and are unchanged.

A second notifier (for example, start-cli replacing a stale one) waits for the first to
exit. While it waits, it logs to stderr and `logs/task-notifier.log`, and records itself
in `state/codex-notifier.lock.waiting`; `health-check.py`'s `codex-task-notifier` check
warns once a live notifier has waited 180 s or more, so a stuck holder cannot stall
delivery unseen. A managed notifier gives up after 600 s
(`SUTANDO_NOTIFIER_LOCK_TIMEOUT`) and its supervisor starts it again. A one-shot
`task-notifier.sh --event <task>` waits only 5 s, so while the managed notifier runs it
always gives up and drops its event; nothing in production calls it.

In both modes the core's notifier delivers each task at most once through one gate: it
takes an atomic per-task claim (`state/codex-delivery-claims/<task-id>`, created on
demand) and then re-checks for a ready result, live or archived, before either
transport. A task that already has a result is skipped; one already claimed by another
notifier, a crash, or an earlier run in the other mode is not delivered again; and a
claim that cannot be taken defers the task to the next cycle, never typing it. The
claim is released only when nothing was delivered, and removed once the task has a
result. Each claim records the core it was handed to (the pane's pid and start time,
stamped just before the hand-off). Because one notifier owns every claim, a claim only
has to survive restarts in sequence: after a core restart, a task that was only typed
into the old core is retyped, as before; one that may have been queued is never retyped
(the daemon may still hold it) but is abandoned at once, with the owner alert below. On start the notifier prunes the
claim of every task that has a ready result, live or archived, whether or not its task
file is still in `tasks/`, and of an abandoned task whose owner alert was written once
its task file has left `tasks/`.

In queue mode the notifier also:

- resolves the core's thread for every task (so `/new` or `/resume` in the TUI is
  followed), from the rollout file the core pane's process tree holds open
  (`codex-observer.mjs --core-thread`). `core.codex.thread` is used only when that
  lookup cannot find the core's thread at all (never when it found a thread with no
  turn yet): if Codex's shared daemon, not the pane's process tree, holds the file,
  the lookup finds nothing (unverified on 0.157+), and without `core.codex.thread`
  every task falls back to send-keys;
- delivers the **first task after every core restart by send-keys**: a fresh thread
  has no rollout file until its first turn, so the lookup finds none open and reports
  no thread (`rc 3`, "0 open"). Queue mode takes over from the second task. This was
  seen on a live scratch Codex core;
- marks the claim "queue" before calling `codex queue`, and sends exactly the prompt
  the send-keys path would type, as one argument;
- does not wait for the pane to go idle and never clears or types into it while
  queueing; pane state is logged as health only;
- falls back to send-keys, with a line in `logs/task-notifier.log`, only when the task
  certainly was not handed off: no thread found, the thread has no persisted turn yet,
  `codex` could not start, or it exited non-zero without acknowledging the queue. That
  fallback types through the usual composer check but skips the wait for an idle core;
- treats a `codex queue` that times out, or acknowledges and then exits non-zero, as
  an unknown outcome: it logs it and waits for the result, and never types the task.

When a claimed task has no result after `SUTANDO_NOTIFIER_COMPLETION_TIMEOUT` (3600 s):

- if it may have been queued (its claim is marked "queue"), the claim is marked
  abandoned, so the task is never typed or queued again; `results/` is left untouched;
  the owner is told out of band, retried every cycle until it is on disk, by a proactive message (`results/proactive-<ts>-<id>.txt`:
  "task <id> was handed to Codex but no reply came back in <N> min; not retried, to
  avoid running it twice"); and the queue moves on;
- if it was only typed, the claim is released and the task is retried as before.

An active `/goal` keeps the TUI from draining its queue (openai/codex#52705).

## AG2 Space MCP without the desktop app

The AG2 Space desktop app writes an `ag2-space` entry into `$CODEX_HOME/config.toml`.
A Codex core set up without the app has no such entry, so it has no AG2 Space
Actions (`room.commons.*` included) until you add the hosted server yourself. Find
the Codex home with `bash scripts/sutando-config.sh core-config-dir-value codex`,
then add this to its `config.toml`:

```toml
[mcp_servers.ag2-space]
url = "https://mcp.ag2.space/mcp"
```

`codex mcp add ag2-space --url https://mcp.ag2.space/mcp` writes the same entry
and starts the sign-in. The server uses OAuth, so no token goes in the file:

```bash
codex mcp login ag2-space   # sign in to AG2 Space in the browser
codex mcp list              # ag2-space: enabled, Auth no longer "Not logged in"
```

That connection acts as you. To act as your agent, call the server's `agent_link`
tool once from a Codex session with the one-time link code on the agent's
settings page in AG2 Space. Restart the core afterwards
(`bash src/agent/start-cli.sh --restart`, from outside the core session).

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
