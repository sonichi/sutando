# Worker pool — design (v1)

**Status:** design, owner-decided 2026-09-03 (PR-triage room). This is step 1
of staging #3604 into PRs against `main`; #3604 stays open as the reference
implementation and is not merged as one piece. It supersedes the
"lead = the runtime daemon" and "lead-managed sizing" placements in #3604's
`docs/lead-follower-pool.md` and Decision 4 of
[`core-pool-standing-sessions.md`](core-pool-standing-sessions.md); every other
decision in that record stands.

The words below are the ones the code uses from now on: **core** (the one
session every install has), **worker** (an extra session the core created),
**pin table** (the owner's room-to-worker bindings, a file), **pool** (core +
workers). There is no router process in v1: routing is data, not a daemon.
"Lead", "follower" and "router" are retired.

## The starting point is zero workers

A fresh install runs the core and nothing else. That is not a degraded pool; it
is the default, and every mechanism below is inert in it. Nothing pool-shaped
runs until the command that creates worker 1, and removing the last worker
leaves the install exactly as it started. Single-worker mode is therefore not a
mode the pool has to detect; it is the absence of a pool.

## Who does what

| component | may do | must not do |
|---|---|---|
| **core** | create, destroy and resize the pool; choose a worker's model; execute every lifecycle command; write the pin table; claim every task no pin sends elsewhere; sweep worker beats, reclaim, revive, report | claim a task pinned to a live worker |
| **worker** | claim tasks for the rooms pinned to it; execute them | claim anything else; create, destroy or reclaim anything |
| **launchd** | keep the processes it was given alive | decide how many there are |
| **app / bridges** | produce intent as owner tasks; render `state/pool-status.json` | call an API into the pool or touch its state |

No component does two of these. Saturation is something the core **reports**
in its status line ("queue waiting, all N workers busy"); that line is an ask
to the owner, never an act. Creating a worker is a spend decision, and it
belongs to the core on the owner's word.

## Routing policy: worker only when obvious, otherwise the core

Routing is the pin table plus one claim rule. A worker claims a task in
exactly two cases:

1. the task's room is **pinned** to that worker, or to a bound set it belongs
   to;
2. the task belongs to a **dedicated** worker's own room.

The core claims everything else. Nothing decides placement at run time: a
worker's candidate set is fixed by the pin table, the core's candidate set is
the complement, and a task is never eligible to both. Two members of a bound
set may both be eligible for one task; the claim rename settles it. There is
no least-loaded fallback, no automatic binding of a room to whichever worker
took its first task, no lane busy-cap, no saturated-pool overflow, no
least-recently-picked tie-break, and no fan-out. Rooms bind only by an explicit
pin. Each of those was a shipped rule in #3604's `_pick()`; each is a later PR
if the owner asks for it, and none is assumed here.

Nothing outside the pin table decides placement. The `target_worker` /
`fan_out` task headers and every writer and reader of them are gone: a sender's
message is not a routing instruction.

## Commands: the owner's intent arrives as an ordinary task

Every pool command is an owner task, written by whatever surface the owner used
(the ag2space app's picker or a create-worker control, a room message, voice, or
the CLI skill), enveloped and verified like any task, with `source:` naming the
intent. One channel, one shape, one code path to test. The core executes all of
them: it is the only claimant no pin can divert, so a command reaches it under
the routing rule above without any special case.

| command | effect |
|---|---|
| pin room R to worker W / to set {W…} · unpin room R | the core rewrites the pin table; workers read it on every claim |
| spawn N · resize to N · remove worker W · set model of W | the core runs the installer (`spawn-worker`) |

What the app needs back is read-only: `state/pool-status.json` (workers, mode,
the core's status line), which the core writes and the bridge already pushes.

## Workers are task-only

A worker has no proactive loop. It starts its task watcher at boot, claims any
eligible task already on disk, and after that every claim and finish is
triggered by a watcher event. The only periodic things in a pool are each
process's heartbeat and the core's sweep. Activation, claim and finish do not
justify a timer, so no `proactive-loop-pool` skill ships.

## Coordination contract (claim-only; the primitives are #3604's)

1. **Claim:** a claimant renames `tasks/task-X.txt` to
   `tasks/task-X.claimed-<name>.txt`, one atomic rename, before working. There
   is no assignment step; eligibility is the pin table.
2. **Done-flags:** `state/cores/<name>/done/task-X.flag` is written before any
   external side effect; the at-most-once floor is unchanged.
3. **Heartbeat / lease:** per-process `.alive`, 30 s beat, 90 s stale.
4. **Reclaim:** the core, in its sweep, reclaims a task claimed by a worker
   whose beat is stale and one claimed with no result evidence, behind the
   done-flag. Workers reclaim nothing.
5. **Stand-in:** while a pinned worker's beat is stale, the core claims that
   room's tasks itself, and stops the moment the beat is fresh. The pin is not
   changed; nothing is loaned or re-bound.

There is no election, no consensus and no degraded mode: the core is the only
process whose absence stops work, which is already true of every install today.

## Lifecycle: one owner, one trigger, one on-disk state per phase

| phase | owner | trigger | state left on disk / user-visible effect |
|---|---|---|---|
| create | core | owner command (`spawn N`) | one plist per worker, the config dir and model captured in it |
| start / stop one worker | launchd, revived by the core | `launchctl kickstart` (launchd defers non-demand spawns, so KeepAlive and timers do not bring a worker back) | on SIGTERM the worker unlinks its `.alive`, so the core stands in at once |
| shutdown of the pool | core | owner command (`resize to 0`) | workers stop; their claimed-but-unfinished tasks are reclaimed by the core, never dropped |
| resume after host sleep | core | beats return | a host sleep is not N dead workers; the core waits for beats before reclaiming (#3782) |
| death of a worker | core | beat stale past the window | the core stands in for its rooms and kickstarts the plist; nothing is lost silently |

## Order of claiming

Each claimant orders its own candidates: `urgent > normal > low`
(`src/task_priority.py`'s contract), and within a priority oldest first by
mtime, then claims the top. Because candidate sets are fixed by the pin table,
priority needs no global view: the "first rename wins regardless of priority"
defect of the 2026-05 leaderless design came from every core competing for
every task, and pins remove the competition.

## What the core and the operator read to judge a worker

A worker's heartbeat is bound to its process, not to its usefulness: it keeps
beating with dead credentials, and a Codex worker's beat is a sidecar of the
pane pid. So `.alive` answers only "is the process up". The signal for "is it
working" is the **age of the oldest unclaimed task in a room pinned to a worker
whose beat is fresh**, which the core computes in its sweep. Classifying a
worker that stopped, carried from #3604's operations section:

| symptom | class | action |
|---|---|---|
| auth errors, `401`, expired credentials | auth state, per process | recycle that worker (`launchctl kickstart -k`); a re-login elsewhere reaches only newly started processes |
| timeouts, 5xx | transport | back off and retry; do not touch the session |
| beat fresh, pinned tasks sit unclaimed | hung session | the core stands in for the room; the kick script un-wedges an idle prompt |
| no tmux session | dead | the core's reconcile kickstarts the plist |

**Codex workers have no in-session task watcher.** A Codex worker learns of a
task only when something types the pool entry at it, so its worst-case claim
latency is the sweep interval, not the sub-second watcher latency a Claude
worker gets, and one wedged inside a turn reads as healthy. v1 states this as a
known limit of Codex workers rather than pretending the task-only,
watcher-driven rule covers them; a Codex-side notifier is its own later PR.

## The durable record of pool timing

`finish_task` appends one line per completed task to `data/pool-metrics.jsonl`
(`task_id`, claimant, `source`, `arrived_at`, `finished_at`, `duration_s`);
arrival is the claimed file's mtime, which survives the rename. Result files
are deleted on delivery and archive mtimes are arrival times, so without this
file "did anything wait on a busy worker" is unanswerable afterwards. It is the
only measurement the core's saturation report and any later scaling decision
can rest on, so it ships with the worker claim path, not with metrics later.

## Registry touchpoints

A worker registers with `role: "worker"` and `pool: <name>` in its instance
manifest; the core discovers workers through the registry, never by scanning
plists. `start_instance` injects manifest identity, so a worker never inherits
the actor identity of whatever created it, and `sutando list` shows the pool
for free once the manifests exist.

## Per-worker model

At creation the owner may choose each worker's model, and may change it in
place afterwards (`--only-core=N`) without disturbing the others. #3604's head
captures one `CLAUDE_CONFIG_DIR` into every plist, which makes the model per
config dir rather than per worker; the installer PR closes that gap by
recording the model per plist. Runtime (Claude / Codex) stays selectable the
same way.

## Packaging

The pool ships as **new files only**: the worker claim path and pin table, the
core's sweep, installer and plists, or as a skill. Existing Sutando files are
not edited except the index and test bookkeeping CI requires when new modules
land under `src/`. Anything that needs an existing file changed is its own PR
with its own reason.

## Staged PRs against main

1. this document;
2. worker side: the claim path (eligibility from the pin table, priority
   order, done-flags) and the worker heartbeat;
3. core side: the pin table writer, the sweep (reclaim, stand-in, revive,
   status line, timing record), with claim and liveness tests;
4. installer and plists, including per-worker model;
5. later, each alone: the app's pin and create-worker controls, dedicated
   workers, pool status push.

Each merges before the next opens. #3604's children re-cut against the new
base after step 3.

## Out of scope for v1

A router process: it earns a process only when a policy needs to see the whole
queue at once (burst consolidation for `[deduped:]`, fairness across sets,
fan-out, auto-scale), and every one of those is out of v1. Also out:
auto-scale in either direction; fan-out; router-minted task ids (the admission
/ census gap stays its own track, and bridges keep minting ids as they do
today); idle-timeout rebalancing and the channel-affinity freshness gate (rooms
bind only by pin, so there is nothing to rebalance); lane routing. Install
troubleshooting (TCC under `~/Documents`, config-dir capture, error-log marks)
and `--only-core` / uninstall semantics belong to the installer PR's own doc.
