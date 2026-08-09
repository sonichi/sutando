# Self-upgrade

Safely upgrade this Sutando checkout to the latest upstream code **without
bricking the running core session** — the "success path" distilled from a real
2026-07-20 upgrade that would otherwise hang (and did, the first time).

**Usage**: `/self-upgrade`

## Why this skill exists

A naive "pull + restart" self-upgrade gets **stuck**, because:

1. `src/restart.sh` ends with `exec bash src/startup.sh`.
2. `src/startup.sh` runs **foreground** work and **foreground-parents the
   credential-proxy** (a `tsx` process that never exits). The open-source core
   is headless: the optional Swift helpers (`ax-read`, `Sutando.app`) are built
   separately by the app packaging/setup workflow, not by core startup.
3. So running `restart.sh` **inline** from the core session never returns —
   the Bash call hangs forever, the task never gets a result, and from the
   owner's side you've "gone stuck."

The fix is simple once you know it: **hand the restart to the same durable tmux
server that owns the core**. A plain `nohup … &` is not enough: the supported
Codex executor tears down that process tree when the tool call ends. A detached
tmux service pane survives that boundary, remains the parent of restarted
services, and lets startup recreate the managed task notifier.

## On activation

### Step 1 — Pull + durable restart handoff (mechanical)

Run the helper. It aborts safely on a dirty tree or a non-fast-forward, pulls
`--ff-only`, and launches `src/restart.sh` in the persistent
**`sutando-services` tmux session**:

```bash
bash skills/self-upgrade/scripts/upgrade.sh          # origin/main
# bash skills/self-upgrade/scripts/upgrade.sh --no-restart   # pull only
```

Exit `0` = upgraded (or already latest); exit `2` = aborted (dirty tree /
not a fast-forward) — surface the reason and stop.

If the diff touched `package*.json` / `tsconfig` / `*.swift` / `requirements`
(the script prints this), a rebuild may be needed. For `*.swift`, rebuild the
optional menu-bar app and `ax-read` through the app setup workflow; core
`startup.sh` intentionally does not build them. For npm deps run `npm ci`
before relying on the TS services.

### Step 2 — Verify + report

```bash
python3 src/health-check.py
```

Expect **"All systems operational."** Confirm the core survived (the restart
log contains `sutando-core already running` — `restart.sh` never touches the
core CLI), the managed `sutando-core-watcher` tmux session exists, and bridges
came back on **new PIDs**. `telegram-bridge` / `slack-bridge` warnings are fine
if they were already optional/unconfigured.

For live-path evidence, submit one task through `POST /task`, write its result,
and confirm `GET /result/<id>` returns that exact body after the restart.

Report to the owner: old → new commit, how many commits, whether a rebuild
was needed, and that the core stayed up.

## Guardrails (learned the hard way)

- **Never run `restart.sh` / `startup.sh` inline** from the core session, and
  do not rely on plain `nohup … &`. Inline = stuck; an executor may reap the
  nohup child. Use the helper's durable tmux handoff.
- **Do NOT hand-kill an active `sutando-services` session** to "tidy up."
  It deliberately parks after startup completes so background bridges keep
  their durable parent. The helper marks the session `done` and only replaces
  that completed session when a later upgrade actually needs another restart.
- **Verify a process is actually yours before killing anything.** `pgrep -f
  watch-tasks-stream` also matches *other* installs (e.g. a `/tmp/…` checkout);
  match the full repo path, not a bare pattern.
- **Clean tree first.** The helper aborts on uncommitted changes rather than
  clobber them; commit or stash before upgrading.

## Iteration log

- v0.2.0 — 2026-07-23 — replace plain `nohup` with a durable
  `sutando-services` tmux handoff that outlives task executors and keeps the
  bridge parent alive after startup; completion markers are isolated per tmux
  socket.
- v0.1.0 — 2026-07-20 — initial. Distilled from a live self-upgrade (8 commits
  behind → 0) where the naive inline restart hung on startup.sh's foreground
  Swift build + credential-proxy hold.
