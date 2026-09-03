---
name: spawn-worker
description: "Leave single-worker mode: spawn one or more pool workers next to the core, or grow an existing pool. Wraps scripts/install-core-pool.sh, which also ensures the pool lead, so one command moves an install into multi-worker mode."
user-invocable: true
---

# Spawn a worker

Sutando ships in **single-worker mode**: the core is the only worker, and no
`com.sutando.core-N` launchd job exists. When the user wants more hands — "spawn a
worker", "add two workers", "go multi-worker" — this skill installs or grows the
lead-follower pool. Every worker is a launchd-managed session sharing this
workspace; the pool lead assigns tasks to them and the core keeps its own duties
(see `docs/lead-follower-pool.md`).

**Usage**: `/spawn-worker [N]` — add one worker (default), or `N` workers.

## What to run

```bash
bash skills/spawn-worker/scripts/spawn-worker.sh --status          # which mode, how many
bash skills/spawn-worker/scripts/spawn-worker.sh --dry-run         # the plan, no changes
bash skills/spawn-worker/scripts/spawn-worker.sh                   # +1 worker
bash skills/spawn-worker/scripts/spawn-worker.sh --count 2         # +2 workers
bash skills/spawn-worker/scripts/spawn-worker.sh --to 3            # exactly 3 workers
```

The script prints `plan: installed=<n> target=<n> mode-after=multi-worker`, runs
`scripts/install-core-pool.sh <target>` (that installer also installs and loads the
pool lead), waits `SUTANDO_SPAWN_WAIT_S` seconds (default 20) for the first beats,
then prints `done: mode=multi-worker workers=<n> live=<k> lead=installed`.

## Steps

1. **Status first.** Run `--status` and tell the user the current mode before
   changing anything. If the pool already has the requested size, say so and stop.
2. **Spawn.** Run the script with the requested count. It takes 20–60 s: the
   installer boots out every pool job and the lead, re-stages them, and loads them
   again. Existing workers' tmux sessions outlive their jobs, so they keep context.
3. **Verify.** Read the `done:` line. `live` counts workers whose heartbeat is
   younger than 90 s. A worker missing from `live` a minute after the spawn is a
   real problem — point the user at `bash scripts/pool-status.sh`, and at
   `docs/lead-follower-pool.md` → "Triaging a follower that stops working".
4. **Report** the before/after mode in one line, e.g. "Was single-worker; now
   multi-worker with 2 workers (both live) and the lead running."

## Guards, by design

- **Runs from the core, never from a worker.** With `SUTANDO_CORE_ID` set the
  script refuses (exit 2): a worker re-running the installer reboots its own
  supervisor.
- **Scale-down is manual.** `--to` below the installed size is refused; removing a
  worker can strand its live claims, so it stays an explicit
  `scripts/uninstall-core-pool.sh` step.
- **`SUTANDO_POOL_MAX`** (default 3) caps the lead's *automatic* growth only. An
  explicit spawn above it works, but the lead will not add more on its own.
- The runtime of new workers follows the installer's default (`claude`, or
  `SUTANDO_POOL_RUNTIME`). To make one worker Codex, use
  `scripts/install-core-pool.sh --only-core=<n> --core-runtime=<n>:codex` after
  the spawn, as the design doc's "Turning on a Codex follower" describes.

## Exit codes

`0` done (or nothing to do), `2` usage or refused, otherwise the installer's own
exit code with the installed count on stderr.
