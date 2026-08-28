# Lead-follower agent pool — design

**Status:** Design (owner go 2026-08-23, Pro-Main: build on Server v0's instance
registry, dedicated long-iteration branch). Supersedes the leaderless design in
`feat/multi-core-channel-affinity` (#880/#884, unmerged) — its claim/lease/
done-flag primitives carry over; its zero-coordination scheduling does not.

## Naming: one core, N workers

**A Sutando install has exactly one core.** The core owns the proactive loop, memory,
`core-status`/heartbeat, cron registration, and the owner relationship. The pool seats are
**workers**: a worker claims a routed task, executes it, and writes a result.

**"Follower" stays — it is the role, not the thing.** A seat is a *follower* with respect to
the lead (it takes assignments and does not schedule), and it *is* a **worker**. What it is
never a *core*: `CLAUDE.md` scopes status/heartbeat/liveness writes to "the single live
Sutando core" and builds the guest-vs-core boundary on that being singular, so calling a seat
a core makes that sentence false. It already costs in practice — a host running
`SUTANDO_CORE_ID=legacy` alongside seats `core-1..3` cannot answer "am I core-1?" from its own
environment.

This also settles the compatibility question: **single-core is the N=0 worker case.** Same
core, same loop, same memory, same owner relationship, zero workers attached. A worker pool is
something a core gains, never something it becomes — installing the pool changes no
single-core semantics, and uninstalling it returns to exactly the prior shape.

**On-disk vocabulary is unchanged in this commit** — `state/cores/<instance>/done/`,
`install-core-pool.sh`, and the `core-N.err` logs keep their current spelling. They are wire
format shared with the reclaim path: if a lead and a worker disagree mid-flight about what a
claim or a done-flag looks like, a live task becomes unreclaimable. That rename belongs in its
own commit with a window that reads both spellings. (`role: "follower"` in the manifest needs
no change — follower is the correct role name.) `lead` → `router`, also proposed, is left open.

## Why lead-follower, not leaderless

The 2026-05 leaderless design made the kernel the scheduler (atomic rename =
claim). Zero coordination code — and therefore nowhere to put policy. Channel
affinity needed bolt-on sticky state, priority ordering was unimplementable
(first rename wins regardless of `priority:`), cross-worker thread consolidation
(`[deduped:]`) had no coordination point, and every watcher event thundered
across N workers.

Scheduling policy wants ONE owner — the same principle the repo already
enforces for adapter policy ("shared policy is core; provider I/O stays at the
edge"), applied to the dispatch layer.

**The convergence that decides the architecture:** the D1 census
(docs/census/d1-identity-census.md) found Discord and Slack minting
`task-<epoch-ms>` from the wall clock, against the ratified invariant "task_id
由 Sutando 拥有" (task_id is owned by Sutando). A lead as the single task
admission point is the structural fix for that census gap AND the worker-pool
coordinator — the two tracks meet here.

## Roles

- **Lead = the runtime daemon** (`src/runtime-api/server.py`). It already owns
  `task.submit`, the request store, the result watchers, and the instance
  registry — it is the only component with a global queue view.
- **Followers = registered instances** (instance_registry manifests). Each
  follower is an ordinary worker session: registered, heartbeating, attachable.
  A follower executes only work the lead assigned to it.

## Coordination contract (filesystem, same primitives as #884)

1. **Admission (new):** bridges hand inbound work to the lead
   (`task.submit` or the tasks/ drop the lead watches); the LEAD mints the
   task id. Bridge-minted wall-clock ids retire per-bridge as each strangler
   slice lands; during migration the lead adopts pre-minted ids unchanged.
2. **Assignment:** lead renames `tasks/task-X.txt` →
   `tasks/task-X.assigned-<instance>.txt` (atomic, crash-safe). Assignment IS
   the schedule: affinity, priority, dedup, and consolidation are lead-side
   policy in one module, testable without any follower running.
3. **Execution claim:** the assigned follower renames to
   `task-X.claimed-<instance>.txt` before working (existing claim shape —
   watchers/archivers keep matching `task-*`).
4. **Done-flags:** unchanged from #884 — write
   `state/cores/<instance>/done/task-X.flag` BEFORE any external side effect;
   helpers check all instance dirs. At-most-once floor survives.
5. **Heartbeat/lease:** existing per-instance `.alive` (30s beat, 90s = dead).
   Lead reclaims assignments whose follower died; followers reclaim nothing
   from each other.

## Degraded mode (no election)

Followers watch the LEAD's heartbeat. If it is stale >90s they fall back to
leaderless atomic-rename claiming of unassigned `tasks/task-*.txt` — the #884
behavior, verbatim — and return to assignment-only the moment the lead's beat
is fresh again. Availability degrades to "no policy" rather than "no service",
and no consensus protocol enters the system. launchd restarts the lead.

## What the lead's policy module owns (one place, finally)

- **Channel affinity:** sticky follower per channel, idle-timeout rebalance
  (the #884 semantics, now a table in one process instead of race-side state).
- **Priority:** `urgent > normal > low` before mtime FIFO — the contract
  `src/task_priority.py` documents but leaderless claiming could not honor.
- **Thread consolidation:** burst detection assigns the whole burst to one
  follower so `[deduped:]` works cross-task.
- **Pool metrics:** the owner's 2026-05-19 quality bar (continuous benchmark:
  claim distribution, head-of-line incidents, duplicate-reply rate,
  per-channel latency) becomes a lead-side counter file — global view, one
  collection point, no cross-worker aggregation.

## Instance registry touchpoints

- Followers register with `role: "follower"` + `pool: <name>` in their
  manifest; the lead discovers them via `list_instances()` + attachable().
- `start_instance` already injects manifest identity (post-#3303) — a follower
  never inherits the caller's actor identity.
- The TUI/`sutando list` shows the pool for free once the manifests exist.

## Iteration plan (long branch, slices)

- **L0 (this commit):** design doc.
- **L1:** lead-side assignment engine + policy module with the leaderless
  fallback, unit-tested against a fake registry; no follower changes.
- **L2:** follower loop honors assignments (skill-level change +
  `claim_task.py` port from #884 with the assigned-prefix step).
- **L3:** launchd pool install scripts (port from #884; plist pitfalls there
  are already solved — permission mode, --add-dir variadic order).
- **L4:** continuous benchmark per the owner's quality bar; live soak with
  N=2 before N=3.
- Each slice lands on this branch; the branch merges to the server branch (or
  main, post-train) only when the owner calls it sound — quality bar, not
  feature count, gates sharing.

## Install troubleshooting (each observed on the first real N=2 install)

| Symptom | Cause | Fix (now automated in `install-core-pool.sh`) |
|---|---|---|
| launchd job exits instantly, `last exit code = 78`, or never spawns | macOS TCC: launchd cannot exec scripts under `~/Documents`, open log files there, or use it as `WorkingDirectory` | Wrapper is staged to `~/.sutando/bin/`, logs go to `~/Library/Application Support/Sutando/logs/`, `WorkingDirectory=$HOME` |
| `Failed to authenticate: OAuth session expired` | Follower defaulted to `~/.claude` while the live session's credentials live in `CLAUDE_CONFIG_DIR` | Installer captures its own `CLAUDE_CONFIG_DIR` into the plist env; preflight warns if no `.credentials.json` there |
| `Unknown command: /proactive-loop-pool` | Pool skill not discoverable in the shared config dir | Installer symlinks `skills/proactive-loop-pool` into `$CLAUDE_CONFIG_DIR/skills/` |
| Old error text after a fix | `core-N.err` accumulates across runs | Append a `=== MARK ===` line, kickstart, read only post-mark lines |

Debug recipe: reproduce outside launchd first (`cd $POOL_REPO_DIR && $POOL_CLAUDE_BIN --dangerously-skip-permissions --add-dir $POOL_WORKSPACE --print "Reply with exactly: BOOT-OK"`) — userland success + launchd failure isolates the plist env; userland failure isolates auth/skill/config.
