# Lead-follower agent pool — design

**Status:** Design (owner go 2026-08-23, Pro-Main: build on Server v0's instance
registry, dedicated long-iteration branch). Supersedes the leaderless design in
`feat/multi-core-channel-affinity` (#880/#884, unmerged) — its claim/lease/
done-flag primitives carry over; its zero-coordination scheduling does not.

## Why lead-follower, not leaderless

The 2026-05 leaderless design made the kernel the scheduler (atomic rename =
claim). Zero coordination code — and therefore nowhere to put policy. Channel
affinity needed bolt-on sticky state, priority ordering was unimplementable
(first rename wins regardless of `priority:`), cross-core thread consolidation
(`[deduped:]`) had no coordination point, and every watcher event thundered
across N cores.

Scheduling policy wants ONE owner — the same principle the repo already
enforces for adapter policy ("shared policy is core; provider I/O stays at the
edge"), applied to the dispatch layer.

**The convergence that decides the architecture:** the D1 census
(docs/census/d1-identity-census.md) found Discord and Slack minting
`task-<epoch-ms>` from the wall clock, against the ratified invariant "task_id
由 Sutando 拥有" (task_id is owned by Sutando). A lead as the single task
admission point is the structural fix for that census gap AND the multi-worker
coordinator — the two tracks meet here.

## Roles

- **Lead = the runtime daemon** (`src/runtime-api/server.py`). It already owns
  `task.submit`, the request store, the result watchers, and the instance
  registry — it is the only component with a global queue view.
- **Followers = registered instances** (instance_registry manifests). Each
  follower is an ordinary core session: registered, heartbeating, attachable.
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

## Channel affinity, as actually implemented (#884, `src/claim_task.py`)

The lead will subsume this policy, but the shipped mechanism is race-side and
worth stating exactly, because two of its properties are commonly misread.

**State.** One file per channel, `state/cores/channel-<id>.handler`, written
atomically (temp + rename) as `{"core_id": ..., "last_handled_at": <epoch>}`.

**Two independent gates.** A follower defers to the recorded handler only when
BOTH hold; they fail for different reasons and protect different things:

| gate | test | protects against |
|---|---|---|
| freshness | `now - last_handled_at < IDLE_THRESHOLD` (30 min) | a live core hoarding a channel it has stopped serving |
| liveness | `.alive` mtime `< ALIVE_THRESHOLD` (90 s) | a **dead** core stranding the channel |

Crash recovery therefore does **not** depend on the idle threshold: a dead
handler releases its channel in 90 seconds no matter how long that window is.
The idle threshold governs only rebalancing among cores that are still alive —
so raising it is far cheaper than it first appears.

**`last_handled_at` is claim-stamped, not activity-stamped.** It is written in
exactly one place — on a successful claim — and never refreshed while the task
runs or when it completes. The value means "when this core last *started* work
on this channel," which has a consequence worth designing around: a core that
spends longer than `IDLE_THRESHOLD` on a single task goes stale *while actively
working on that channel's task*, and the next message races freely to a core
with none of the conversation. The busy core stays perfectly **alive**
throughout — the heartbeat is a sidecar and keeps beating — so the failure is
not "busy mistaken for idle"; activity is never measured at all.

The minimal correction is to stamp completion as well as claim, converting the
window from "since work started" to "since work ended". Making the number
larger treats the symptom and leaves the wrong event being measured.

`SUTANDO_CORE_IDLE_THRESHOLD_SEC` overrides the window; non-numeric and
non-positive values fall back to the default rather than disabling affinity.

## What the lead's policy module owns (one place, finally)

- **Channel affinity:** sticky follower per channel, idle-timeout rebalance
  (the #884 semantics above, now a table in one process instead of race-side
  state — which is also the natural place to fix the claim-stamp defect).
- **Priority:** `urgent > normal > low` before mtime FIFO — the contract
  `src/task_priority.py` documents but leaderless claiming could not honor.
- **Thread consolidation:** burst detection assigns the whole burst to one
  follower so `[deduped:]` works cross-task.
- **Pool metrics:** the owner's 2026-05-19 quality bar (continuous benchmark:
  claim distribution, head-of-line incidents, duplicate-reply rate,
  per-channel latency) becomes a lead-side counter file — global view, one
  collection point, no cross-core aggregation.

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

## Operations

### Recovery does not use launchd timers

Measured on this host during a deliberate kill drill: a killed follower with
`KeepAlive: true` stayed dead indefinitely (`runs` frozen, `pended nondemand
spawn = inefficient`, exit 0), and the watchdog's own `StartInterval: 180`
never fired unaided over 7 minutes (`pended nondemand spawn = interval`).
launchd defers *non-demand* spawns here; only `launchctl kickstart` — a demand
spawn — reliably starts a job. Anything that relies on KeepAlive or a plist
timer to bring a core back is decorative.

So recovery is driven by the **lead**, the one process provably running (2s
sweep). Every 60s it reconciles the installed `com.sutando.core-*` plists
against live tmux sessions and kickstarts whichever core has no session.
`scripts/kick-pool.sh` holds that logic and additionally un-wedges a session
that is alive but idle at the REPL.

Verify it is alive by its own output, never by log presence:

    grep 'recovery:' <workspace>/logs/pool-lead.log | tail -3
    # recovery: ok (3 session(s) healthy)      <- idle heartbeat, emitted every pass
    # recovery: core-3: NO SESSION (dead) -> launchctl kickstart

The idle heartbeat exists because a sweep that logs only when it acts is
indistinguishable from a sweep that stopped running — the failure mode that
hid a dead watchdog for three months.

### Triaging a follower that stops working

Classify before acting; the deciding question is *would a newly started
process succeed?*

| Symptom | Class | Action |
|---|---|---|
| `401`, credentials expired, auth errors | auth-state — per-process, not shared | Recycle that session (`launchctl kickstart -k gui/$(id -u)/com.sutando.core-N`). Retrying re-hits the same dead auth; a re-login elsewhere only affects *newly started* processes |
| Timeouts, 5xx, network errors | transport | Back off and retry; do not touch the session |
| Heartbeat fresh but assignments sit unclaimed | hung session | The lead's stuck-assignment reclaim repools it; `kick-pool.sh` un-wedges an idle REPL |
| No tmux session | dead core | The lead's 60s reconcile kickstarts it (~55s observed end to end) |

A follower's heartbeat is pid-bound, so it keeps beating while auth is dead —
use assigned-but-unclaimed age as the signal, not the `.alive` file.

### Scaling

`bash src/startup.sh --pool N` (or `--pool auto`, which starts at 2 and lets
the lead grow the pool) installs/resizes and ensures the lead is running.
Scale-up is automatic under saturation up to `SUTANDO_POOL_MAX` (default 3);
**scale-down stays manual** because booting out a core can strand its live
claims. `SUTANDO_AFFINITY_BUSY_MAX` (default 3) sets how backlogged a channel's
handler must be before affinity yields — lower favors latency, higher favors
conversational continuity; `continuity_breaks` in the pool metrics measures
what that choice costs.

### Turning on a Codex follower

The runtime dimension (`--runtime` / `--core-runtime`) declares which CLI a core
runs. Two things make it usable on a running pool:

    # convert core 2 to codex, in place — the lead and cores 1,3 keep working
    bash scripts/install-core-pool.sh --only-core=2 --core-runtime=2:codex

    # remove it again — plist, tmux session AND the stale beat, in one step
    bash scripts/uninstall-core-pool.sh --only-core=2

`--only-core` converts a core in place but **cannot resize the pool** — it
refuses when the N given differs from the installed size. Adding a fourth core
is therefore a full install, which boots out and re-bootstraps every core and
the lead. That is less disruptive than it sounds: the tmux sessions outlive the
launchd jobs, so running sessions keep their context and the job is only a
supervisor. Plan for the churn anyway; nothing guarantees that ordering.

### Measuring the pool

`finish_task` appends one line per completed task to `data/pool-metrics.jsonl`:
`task_id`, `core`, `source`, `arrived_at`, `finished_at`, `duration_s`. Arrival
comes from the claimed file's mtime, which survives the assign/claim renames, so
the duration is measured rather than inferred.

Two fields are deliberately absent. `assigned_at` and `claimed_at` would need
the lead to stamp them — the renames preserve mtime, so those instants do not
exist by the time a follower finishes, and a field that cannot be populated
honestly is worse than no field.

This is the only durable record of pool timing. Result files are deleted once a
bridge delivers them, and archive mtimes are arrival times, not completion
times — so without this file, questions like "did anything wait on a busy core"
and "what does N=3 buy over N=1" are unanswerable after the fact.

Without `--only-core` the installer boots out the lead and every core, so
changing one follower restarts the whole pool. Without the teardown path,
`launchctl bootout` alone leaves the plist behind — the recovery sweep revives
any installed plist whose session is gone — and a left-behind
`state/cores/core-N.alive` keeps the lead assigning to a core that no longer
exists.

How the session is driven is owned by `scripts/pool-runtime-drive.sh`, sourced
by both `pool-core-wrapper.sh` (the in-session sweep) and `kick-pool.sh` (the
watchdog). Recognition is positive and fails closed: a session is typed into
only when the pane shows *that* runtime's idle prompt. A pane nobody recognizes,
a plist whose runtime is unreadable or unknown, and a codex startup dialog are
all skipped and logged — never typed into with the other runtime's text.

**Known gap — a Codex follower can look alive while missing work.** Its beat is
a sidecar bound to the pane pid, so `.alive` stays fresh regardless of whether
the model is reading anything. It has no in-session task watcher and
`task-notifier.sh` has no pool mode, so it learns about an assignment only when
something types the pool entry at it: the wrapper's own sweep (300s) or the
watchdog (180s on this host). Worst-case assignment latency is therefore the
sweep interval, not the sub-second watcher latency a Claude follower gets, and
a Codex core wedged *inside* a turn is invisible to both — `esc to interrupt`
reads as healthy work. Use assigned-but-unclaimed age, not `.alive`, to judge it.
