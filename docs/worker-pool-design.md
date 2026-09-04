# Worker pool — router design (v1)

**Status:** design, owner-decided 2026-09-03 (PR-triage room). This is step 1
of staging #3604 into PRs against `main`; #3604 stays open as the reference
implementation and is not merged as one piece. It supersedes the
"lead = the runtime daemon" and "lead-managed sizing" placements in #3604's
`docs/lead-follower-pool.md` and Decision 4 of
[`core-pool-standing-sessions.md`](core-pool-standing-sessions.md); every other
decision in that record stands.

The words below are the ones the code uses from now on: **core** (the one
session every install has), **worker** (an extra session the core created),
**router** (the process that assigns queued tasks to workers), **pool** (core +
workers + router). "Lead" and "follower" are retired.

## The starting point is zero workers

A fresh install runs the core and nothing else. That is not a degraded pool; it
is the default, and every mechanism below is inert in it. The router does not
exist until the first worker does: the command that creates worker 1 installs
the router with it, and removing the last worker stops the router. Single-worker
mode is therefore not a mode the pool has to detect; it is the absence of a
pool.

## Who does what

| component | may do | must not do |
|---|---|---|
| **core** | create, destroy and resize the pool; choose a worker's model; execute every lifecycle command | route tasks once a router exists |
| **router** | assign a queued task; reclaim (dead, stuck, claimed-without-result); revive a worker launchd let die; report status | create or destroy a worker, change a model, drop a task, spend |
| **launchd** | keep the processes it was given alive | decide how many there are |
| **app / bridges** | produce intent as owner tasks; render `state/pool-status.json` | call an API into the pool or touch its state |

No component does two of these. The router's scaling role is to **report**
saturation ("queue waiting, all N workers busy") in its status line; that line
is an ask to the owner, never an act. #3604's auto-scale-up branch (the router
running the installer under `--pool-max`) is out of v1 on purpose: creating a
worker is a spend decision, and it belongs to the core on the owner's word.

## Routing policy: worker only when obvious, otherwise the core

The router assigns a task to a worker in exactly two cases:

1. the task's room is **pinned** to a worker, or to a bound set, and that worker
   is alive and claiming (a set picks its least-loaded live member);
2. the task belongs to a **dedicated** worker's own room.

Everything else goes to the core. There is no least-loaded fallback, no
automatic binding of a room to whichever worker took its first task, no lane
busy-cap, no saturated-pool overflow, no least-recently-picked tie-break, and no
fan-out. Rooms bind only by an explicit pin. Each of those was a shipped rule in
#3604's `_pick()`; each is a later PR if the owner asks for it, and none is
assumed here.

Nothing outside the router decides placement. The `target_worker` / `fan_out`
task headers and every writer and reader of them are gone: a sender's message is
not a routing instruction.

## Commands: the owner's intent arrives as an ordinary task

Every pool command is an owner task, written by whatever surface the owner used
(the ag2space app's picker or a create-worker control, a room message, voice, or
the CLI skill), enveloped and verified like any task, with `source:` naming the
intent. One channel, one shape, one code path to test.

| command | executed by | effect |
|---|---|---|
| pin room R to worker W / to set {W…} · unpin room R | router | affinity table |
| spawn N · resize to N · remove worker W · set model of W | core | runs the installer (`spawn-worker`) |

Once workers exist, the routing rule above already carries a lifecycle command
to the core, because no pin names a worker for it. The router applies pin
commands itself because the affinity table is the only state they touch.

What the app needs back is read-only: `state/pool-status.json` (workers, mode,
the router's status line), which the router writes and the bridge already
pushes.

## Workers are task-only

A worker has no proactive loop. It starts its task watcher at boot, drains any
assignment already on disk, and after that every claim and finish is triggered
by a watcher event. The only periodic things in a pool are each process's
heartbeat and the router's sweep. Activation, claim and finish do not justify a
timer, so no `proactive-loop-pool` skill ships.

## Coordination contract (kept from #3604, unchanged)

1. **Assignment:** the router renames `tasks/task-X.txt` to
   `tasks/task-X.assigned-<worker>.txt`, one atomic rename. Assignment is the
   schedule.
2. **Claim:** the assigned worker renames it to `task-X.claimed-<worker>.txt`
   before working. Workers claim only what was assigned to them.
3. **Done-flags:** `state/cores/<worker>/done/task-X.flag` is written before any
   external side effect; the at-most-once floor is unchanged.
4. **Heartbeat / lease:** per-process `.alive`, 30 s beat, 90 s stale. The router
   stamps its own `pool-router.alive` each sweep.
5. **Reclaim:** the router reclaims an assignment whose worker's beat is stale,
   one assigned but never claimed, and one claimed with no result evidence.
   Workers reclaim nothing from each other.
6. **Degraded mode:** when the router's beat is stale, workers fall back to
   leaderless atomic-rename claiming of unassigned tasks, and return to
   assignment-only the moment the beat is fresh. No election, no consensus.

## Lifecycle: one owner, one trigger, one on-disk state per phase

| phase | owner | trigger | state left on disk / user-visible effect |
|---|---|---|---|
| create | core | owner command (`spawn N`) | one plist per worker, the config dir and model captured in it; worker 1 also installs the router |
| start / stop one worker | launchd, revived by the router | `launchctl kickstart` (launchd defers non-demand spawns, so KeepAlive and timers do not bring a worker back) | on SIGTERM the worker unlinks its `.alive`, so the router reclaims at once |
| shutdown of the pool | core | owner command (`resize to 0`) | router stops last, so nothing is assigned to a worker that is going away; assigned-but-unclaimed tasks return to the queue, never dropped |
| resume after host sleep | router | beats return | a host sleep is not N dead workers; the router waits for beats before reclaiming (#3782) |
| death of a worker | router | beat stale past the window | its assignments come back to the queue within about a minute; the router kickstarts the plist; nothing is lost silently |

## Order of assignment

The router assigns unassigned tasks in priority order, `urgent > normal > low`
(`src/task_priority.py`'s contract), and within a priority oldest first by
mtime. This is the one scheduling rule leaderless claiming could never honor
and the reason a router exists at all; it is carried from #3604 unchanged.

## What the router and the operator read to judge a worker

A worker's heartbeat is bound to its process, not to its usefulness: it keeps
beating with dead credentials, and a Codex worker's beat is a sidecar of the
pane pid. So `.alive` answers only "is the process up". The signal for "is it
working" is **assigned-but-unclaimed age**, and the router's stuck-assignment
reclaim is what acts on it. Classifying a worker that stopped, carried from
#3604's operations section:

| symptom | class | action |
|---|---|---|
| auth errors, `401`, expired credentials | auth state, per process | recycle that worker (`launchctl kickstart -k`); a re-login elsewhere reaches only newly started processes |
| timeouts, 5xx | transport | back off and retry; do not touch the session |
| beat fresh, assignments sit unclaimed | hung session | the router's stuck reclaim repools the work; the kick script un-wedges an idle prompt |
| no tmux session | dead | the router's reconcile kickstarts the plist |

**Codex workers have no in-session task watcher.** A Codex worker learns of an
assignment only when something types the pool entry at it, so its worst-case
claim latency is the sweep interval, not the sub-second watcher latency a
Claude worker gets, and one wedged inside a turn reads as healthy. v1 states
this as a known limit of Codex workers rather than pretending the task-only,
watcher-driven rule covers them; a Codex-side notifier is its own later PR.

## The durable record of pool timing

`finish_task` appends one line per completed task to `data/pool-metrics.jsonl`
(`task_id`, worker, `source`, `arrived_at`, `finished_at`, `duration_s`);
arrival is the claimed file's mtime, which survives the renames. Result files
are deleted on delivery and archive mtimes are arrival times, so without this
file "did anything wait on a busy worker" is unanswerable afterwards. It is the
only measurement the router's saturation report and any later scaling decision
can rest on, so it ships with the router PR, not with metrics later.

## Registry touchpoints

A worker registers with `role: "worker"` and `pool: <name>` in its instance
manifest; the router discovers workers through the registry, never by
scanning plists. `start_instance` injects manifest identity, so a worker never
inherits the actor identity of whatever created it, and `sutando list` shows
the pool for free once the manifests exist.

## Per-worker model

At creation the owner may choose each worker's model, and may change it in
place afterwards (`--only-core=N`) without disturbing the others. #3604's head
captures one `CLAUDE_CONFIG_DIR` into every plist, which makes the model per
config dir rather than per worker; the installer PR closes that gap by
recording the model per plist. Runtime (Claude / Codex) stays selectable the
same way.

## Packaging

The pool ships as **new files only**: router module and daemon, worker claim
path, installer and plists, or as a skill. Existing Sutando files are not
edited except the index and test bookkeeping CI requires when new modules land
under `src/`. Anything that needs an existing file changed is its own PR with
its own reason.

## Staged PRs against main

1. this document;
2. worker side: the claim path and the worker heartbeat;
3. router: module, daemon, the two-case pick, the three reclaims, the status
   line, with assignment and liveness tests;
4. installer and plists, including per-worker model;
5. later, each alone: pins and dedicated workers, pool status and metrics.

Each merges before the next opens. #3604's children re-cut against the new
base after step 3.

## Out of scope for v1

Auto-scale in either direction; fan-out; burst consolidation for `[deduped:]`;
router-minted task ids (the admission / census gap stays its own track, and
bridges keep minting ids as they do today); idle-timeout rebalancing and the
channel-affinity freshness gate (rooms bind only by pin, so there is nothing to
rebalance); lane routing. Install troubleshooting (TCC under `~/Documents`,
config-dir capture, error-log marks) and `--only-core` / uninstall semantics
belong to the installer PR's own doc.
