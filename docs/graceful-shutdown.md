# Graceful shutdown — the sentinel, and which path actually signals you

Referenced from CLAUDE.md "Graceful shutdown". Detail lives here so the
always-loaded file stays within its size budget (see #3016).

**At the top of every proactive-loop pass, before starting new work, check the shutdown sentinel:**

```bash
python3 src/shutdown.py check   # exit 0 = shutting down, 1 = not
```

If it exits 0, an intentional **stop** has been signalled. **Finish the current task, do NOT start a new one, write `{"status":"idle"}` to core-status, and end the loop cleanly** — instead of being killed mid-task, which orphans the task until the result-watcher timeout (the visible "no response" delay). The core launchers clear the sentinel once a core session is verified live (`src/agent/*/cli/start-cli.sh`), so the next session runs normally — `startup.sh` does not clear it. Helpers: `src/shutdown.py` (`is_shutting_down()` / `shutdown_info()` for Python callers).

**Which path actually reaches you — the two are not the same, and only one is a clean-exit signal:**

| path | sentinel | what the core should expect |
|---|---|---|
| `restart.sh --stop-only` / explicit stop | marked before any stop and **left set** (the script exits before any clear) | you WILL see it on your next pass — exit cleanly |
| menu-bar / chat **Stop** (`stop-core.sh`) | marked at `stop-core.sh` before the kill, and **left set** | the core is killed directly, so treat the sentinel as the durable record that the stop was deliberate — not as an observation window |
| plain `restart.sh` | marked before the stop, **cleared ~3s later** before `startup.sh` | you will almost certainly NOT see it, and that is intended |

**Which file the mark lands on follows the scope.** `--scope all` marks the workspace-wide `state/shutdown.sentinel`, which every watcher on the workspace consults. The default `--scope core` marks only **this instance's** gate, `state/watch-tasks-stream[-<instance-key>].shutdown.sentinel`, beside its own watcher record and keyed the same way (`src/util_paths.py` owns both paths; `shutdown.py --gate instance|workspace` selects). A core restart therefore never holds a peer worker's intake. `shutdown.py check` / `is_shutting_down()` read both gates for the caller's identity, and a launcher's bare `shutdown.py clear` lifts both.

Plain restart does not stop the core at all — no core pattern appears in `STOP_PATTERNS`, so the core **survives**; with the default `--scope core` it hands over only this instance's own task watcher and heartbeat writer, and the services around it restart only under `--scope all` (what `stop.sh`, the desktop Restart and the self-upgrade pass). The clear at `:109` exists precisely so that surviving core does not read a restart as "shut down". So a per-pass check (~5 min cadence) against a ~3s window is not a missed signal; there is deliberately no signal to catch on that path.

The gate that operates on **both** paths is the watcher's intake check (`intake_gated` in `watch-tasks-stream.sh`), which **holds** a task while its own instance gate or the workspace-wide gate is present and dispatches it once the gate lifts — fswatch does not replay, so a dropped event would be a lost task for a watcher that survives the window.
