# Worker pool — design (v1)

**Status:** normative design, owner-decided 2026-09-07 (Pro-Main room). This is
step 1 of staging #3604 into PRs against `main`; #3604 stays open as a reference
implementation and is not merged as one piece. The record behind every decision
here — what it supersedes, what was rejected, and the measurements that forced
each choice — is [`worker-pool-design-notes.md`](worker-pool-design-notes.md).
This file carries the contract only. Where the two disagree, this file wins.

## Summary

A Sutando install runs one **pool supervisor**: a local process that holds no
LLM session and depends on none. It is the only scheduler. It owns routing, the
task lease, worker lifecycle, pool membership, the pin table, reconciliation of
expired leases, and status reporting.

Every agent session — the core agent and every worker agent — is an **executor
only**. An executor is offered a task, accepts it, runs it, and reports the
outcome. It does not route, does not assign, does not reclaim, and does not
decide who else may run.

All task control state lives in **one transactional store**, a local SQLite
database owned by the supervisor. Task bodies stay as files under
`<workspace>/tasks/`; only control metadata is in the store. Every transition is
a single conditional `UPDATE`, so two parties cannot both believe they own a
task, and a crash between any two writes leaves the store in a state the
supervisor can finish on restart.

The starting point is zero workers. A fresh install runs the supervisor and the
core agent and nothing else; every worker-shaped mechanism below is inert until
the owner's first create-worker command, and removing the last worker returns
the install to exactly that state.

## Components and ownership

| component | responsibilities | must not |
|---|---|---|
| **pool supervisor** | the only scheduler. Routes each task to exactly one target; opens, renews and expires the lease; drives worker lifecycle (create, start, shutdown, resume, kick); owns pool membership and the pin table; reconciles expired leases and re-offers; writes `state/pool-status.json` | run an LLM session, or depend on one being responsive for any control action |
| **core agent** | an executor. Runs tasks for rooms with no binding, and every task the owner explicitly directs to it | route, assign, lease, reclaim, sweep, or hold any pool control state |
| **worker agent** | an executor. Runs tasks bound to it or addressed to it | route, assign, lease, reclaim, or claim anything it was not offered |
| **runtime adapter** | translates the executor interface below into one runtime's mechanics (Claude, Codex App Server, ACP). Delivers offers, reports accept/start/completion events, answers health probes | make routing or admission decisions; leak runtime-specific vocabulary above the interface |
| **app / bridges** | submit tasks as envelopes; render `state/pool-status.json` and the owner's choices | call into the pool's state, or write any control field the supervisor owns |

The core agent is an executor only. The supervisor and the core agent may run on
the same machine and inside the same Sutando runtime server, but their
**lifecycles are separate**: the supervisor survives a core restart, a core
model outage, and a core quota stall, because no control action passes through
an LLM session.

**No component holds two of these rows, and the scheduler row is the one that
decides where the split falls: the scheduler must depend on no resource that a
task it schedules can exhaust.** Quota is per account, so an out-of-credits
outage takes every LLM session on the host dark together — the core agent's
included — and a control plane sited inside one of those sessions goes dark in
exactly the outage it exists to act in. This is the principle that already keeps
a resource-exhaustion report out of the exhausted session, applied one row
further: to the scheduler itself.

Saturation is something the supervisor **reports** in `state/pool-status.json`.
Creating a worker is a spend decision and requires an owner command.

## Task state machine

One table, owned by the supervisor, in a local SQLite database under
`<workspace>/state/pool/pool.sqlite3`:

```sql
CREATE TABLE tasks(
  task_id          TEXT PRIMARY KEY,   -- canonical task ID, from the `id:` header
  room_id          TEXT,
  requested_worker TEXT,
  assigned_worker  TEXT,
  state            TEXT NOT NULL,      -- PENDING|OFFERED|ACCEPTED|RUNNING|SUCCEEDED|FAILED
  lease_owner      TEXT,
  lease_until      INTEGER,            -- Unix seconds; NULL in a terminal state
  attempt          INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL,
  accepted_at      INTEGER,
  finished_at      INTEGER
);
```

States: `PENDING` → `OFFERED` → `ACCEPTED` → `RUNNING` → `SUCCEEDED` | `FAILED`.
`SUCCEEDED` and `FAILED` are terminal. `PENDING` is the only re-offerable state.

Every transition is one conditional `UPDATE`. The `WHERE` clause is the
concurrency control: a transition that matches zero rows did not happen, and the
caller must treat that as the authoritative answer rather than retrying blind.

| # | transition | performed by | transaction |
|---|---|---|---|
| 1 | ingest → `PENDING` | supervisor | `INSERT INTO tasks(task_id, room_id, requested_worker, state, attempt, created_at) VALUES(?,?,?, 'PENDING', 0, ?) ON CONFLICT(task_id) DO NOTHING` |
| 2 | `PENDING` → `OFFERED` | supervisor | `UPDATE tasks SET state='OFFERED', assigned_worker=?, lease_owner=?, lease_until=? WHERE task_id=? AND state='PENDING'` |
| 3 | `OFFERED` → `ACCEPTED` | supervisor, on the executor's accept | `UPDATE tasks SET state='ACCEPTED', lease_owner=?, lease_until=?, accepted_at=? WHERE task_id=? AND attempt=? AND state IN ('PENDING','OFFERED')` |
| 4 | `ACCEPTED` → `RUNNING` | supervisor, on the executor's start event | `UPDATE tasks SET state='RUNNING', lease_until=? WHERE task_id=? AND state='ACCEPTED' AND lease_owner=?` |
| 5 | lease renewal | supervisor, on the executor's heartbeat event | `UPDATE tasks SET lease_until=? WHERE task_id=? AND lease_owner=? AND state IN ('ACCEPTED','RUNNING')` |
| 6 | `RUNNING` → `SUCCEEDED` | supervisor, on the executor's completion event | `UPDATE tasks SET state='SUCCEEDED', finished_at=?, lease_owner=NULL, lease_until=NULL WHERE task_id=? AND state='RUNNING' AND lease_owner=?` |
| 7 | `RUNNING` → `FAILED` | supervisor, on the executor's failure event | `UPDATE tasks SET state='FAILED', finished_at=?, lease_owner=NULL, lease_until=NULL WHERE task_id=? AND state='RUNNING' AND lease_owner=?` |
| 8 | lease expiry → `PENDING` | supervisor reconciliation | `UPDATE tasks SET state='PENDING', assigned_worker=NULL, lease_owner=NULL, lease_until=NULL, attempt=attempt+1 WHERE task_id=? AND state IN ('OFFERED','ACCEPTED','RUNNING') AND lease_until < ?` |
| 9 | owner cancel | supervisor, on a `pool_command` task | `UPDATE tasks SET state='FAILED', finished_at=?, lease_owner=NULL, lease_until=NULL WHERE task_id=? AND state NOT IN ('SUCCEEDED','FAILED')` |

Three properties follow from the table and are the reason it is shaped this way.

**`attempt` is the anti-replay token.** An offer carries the `attempt` value it
was made under, and the executor echoes it in its accept. Transition 3 matches on
that value, so an accept produced under an attempt the supervisor has already
expired (transition 8 incremented it) matches zero rows and is refused. Without
it, a late accept from an executor that was slow rather than dead would take a
lease the supervisor has already re-offered.

**`lease_owner` is the executor identity, not the room's binding.** Transitions
4 through 7 all match on it, so a report from any other executor about that task
matches zero rows. `assigned_worker` records intent and is what the status
surface reads; `lease_owner` records possession and is what the transitions
guard on.

**A non-terminal row always carries a `lease_until` in the future or is
reconcilable.** There is no state in which a task is owned by nobody and also
not re-offerable: `PENDING` has no lease and is re-offerable, and every other
non-terminal state carries a lease that either renews or expires into transition
8. That is the invariant the reconciliation backstop enforces and the transitions
test pins.

**Task bodies are not in the store.** The store holds control metadata; the task
body stays in its file, and the result stays in `<workspace>/results/`. The
supervisor never parses a task body for routing.

## Routing table

The supervisor makes one routing decision per task, once, and takes the lease in
the same pass. There is no second party evaluating the same question, so no
schedule exists in which two candidates both decline.

**Message text never participates in routing.** Routing reads the envelope's
verified fields and the pin table, and nothing else. A room message whose body
says "send this to worker-2" is an ordinary task.

| condition | target | store effect |
|---|---|---|
| `requested_worker` names a worker, and that worker is `HEALTHY` | that worker | transition 2 with `assigned_worker` = that worker |
| `requested_worker` names a worker that is not `HEALTHY` | none | the row stays `PENDING`; the room is reported as waiting |
| `requested_worker` names the core (every `pool_command` envelope does) | the core agent | transition 2 with `assigned_worker='core'` |
| no `requested_worker`, the room is bound in the pin table, and its bound worker is `HEALTHY` | that worker | transition 2 with `assigned_worker` = the bound worker |
| no `requested_worker`, the room is bound, and no bound worker is `HEALTHY` | none | the row stays `PENDING`; the room is reported as waiting |
| no `requested_worker`, the room is **unbound** (the pin table names no worker for it) | the core agent | transition 2 with `assigned_worker='core'` |
| the pin table is missing, unreadable, or unparseable | the core agent | the reader answers "not bound", so work continues |

`requested_worker` overrides an ordinary pin: it is the server's decision,
written at ingress into the envelope the server then stamps, and the bridge
records it verbatim. A sender-controlled routing header is not honoured. An
unverified envelope is never worker work — see **Pool membership and the pin
table**.

When more than one target could take one task — a room bound to a set of
workers, or a re-offer racing a late accept — the **lease decides**. Transition
2 matches one row; the loser matches zero and takes no action.

**A bound room whose workers are all unavailable stays `PENDING`, and that is a
product decision, not a failure.** Nothing else takes the work: no other worker,
and not the core agent. Because the wait is deliberate, it must be visible. The
supervisor reports the room in `state/pool-status.json` and the app renders it
as:

> **Room is waiting for worker-2** — Rebind / Restart / Process with core

Those three are owner commands, and each is an ordinary task carrying
`pool_command`:

| choice | `pool_command` | what the supervisor does |
|---|---|---|
| **Rebind** | `pin` | rewrites the room's entry in the pin table, then re-routes its `PENDING` rows on the next pass |
| **Restart** | `kick` | drives the worker's health transition (see **Health model and reconciliation**); the room's rows stay `PENDING` until the worker reaches `HEALTHY` |
| **Process with core** | `run-on-core` | routes that room's `PENDING` rows to the core agent for this occurrence, leaving the binding intact |

Without an owner choice the rows stay `PENDING` indefinitely, which is visible
and recoverable. Routing them somewhere the owner did not ask for is neither.

## Executor interface and runtime adapters

One interface, implemented once per runtime — `Executor.offer(task)`,
`accept(task)`, `start(task)`, `cancel(task)`, `events()`, `health()`. The
supervisor speaks only this; runtime differences live below it.

| call | direction | contract |
|---|---|---|
| `offer(task)` | supervisor → adapter | deliver the offer, carrying the canonical task ID, the body's path and the `attempt` value. Returns only whether delivery was attempted; it is not an accept |
| `accept(task)` | adapter → supervisor | the executor has taken the task. Carries the canonical task ID and the echoed `attempt`. Drives transition 3 |
| `start(task)` | adapter → supervisor | execution has begun. Drives transition 4 |
| `cancel(task)` | supervisor → adapter | stop work on this task. Best-effort; the supervisor does not wait on it before transition 9 |
| `events()` | adapter → supervisor | the stream carrying start, heartbeat, completion and failure. Drives transitions 4 through 7 |
| `health()` | supervisor → adapter | answers the executor's health state for the model below. Must not require a model call to answer `UNAVAILABLE` |

**An executor must explicitly accept.** Delivery is not admission: an offer that
is delivered and never accepted expires with its lease and is re-offered. This
is what removes the delivery deadlock in which an executor is told about work it
is structurally unable to take.

| adapter | delivery | accept / start | health |
|---|---|---|---|
| **Claude** | the session's Monitor / TUI channel | the session reports accept and start over the same channel | process liveness plus the adapter's own probe |
| **Codex App Server** | the supervisor creates the turn through the App Server. **A Codex worker needs no in-session watcher** | the App Server's turn lifecycle is the accept and start signal | App Server reachability plus turn state |
| **ACP** | `session/prompt` | the session's prompt lifecycle | session reachability |

Because delivery is the adapter's problem, a runtime with no in-session watcher
is a first-class worker rather than a documented limitation.

## Worker lifecycle

Every phase has one owner, one trigger, and one recorded state. The owner is the
supervisor in every row; the trigger is always an owner command arriving as an
ordinary task carrying `pool_command`, from any surface (the app, a room
message, voice, or the CLI skill). One channel, one shape, one code path.

| phase | trigger | what the supervisor does | recorded state |
|---|---|---|---|
| **create** | `create-worker` / `resize` up | renders the worker's supervision unit and config, registers the instance with `role: "worker"` and `pool: <name>` | membership record present; worker `UNAVAILABLE` until its first healthy probe |
| **start** | `create-worker`, `resume`, or reconciliation finding the unit down | starts the worker's process through its supervision unit | worker `UNAVAILABLE` → `HEALTHY` on the first healthy probe |
| **shutdown** | `remove-worker` / `resize` down | in this order: stop the process; expire or cancel the leases it holds so their rows return to `PENDING`; remove the worker from every room's binding; deregister it | rows re-offerable; rooms left with no binding are unbound and route to the core agent |
| **resume** | host wake, or `resume` | waits for a healthy probe before offering. A host sleep is not N dead workers | worker `UNAVAILABLE` → `HEALTHY` |
| **kick** | `kick` (the status surface's **Restart**) | `WEDGED` → `PROBING`, then one synthetic health task | see the health model below |

**Shutdown order is the contract.** Stopping the process first is what makes the
lease expiry safe: expiring a lease a live worker still holds would let the
re-offer and the original worker run the same task. Rewriting the bindings
before the process is stopped would let the worker act on a binding that no
longer names it.

`resize to 0` is the shutdown row applied to every worker. It leaves every room
unbound, so every room routes to the core agent, and the install is again what
it was before the first worker.

## Health model and reconciliation

A worker is in exactly one state, computed by the supervisor and published in
`state/pool-status.json`:

| state | meaning | routing effect |
|---|---|---|
| `HEALTHY` | the adapter answers `health()` and the worker accepts offers | eligible |
| `UNAVAILABLE` | the process is down, unreachable, or has never probed healthy | not eligible; the supervisor restarts the unit |
| `WEDGED` | the process is up and answering, and it has failed to accept or to complete offers | not eligible; needs an owner **Restart** |
| `QUIESCED` | the runtime reported resource exhaustion (out of credits) with a reset time | not eligible until the reported reset time passes |

`QUIESCED` is set from what the adapter observes, never from a model call, and
carries the provider's own reported reset time. When that time passes the worker
returns to `UNAVAILABLE` and the ordinary probe decides. Failing toward
re-probing is deliberate: a worker stranded by a record nobody clears is
invisible, because it is up and simply never runs anything.

**The kick cycle is the only exit from `WEDGED`, and it is commanded.** A wedged
worker cannot demonstrate recovery through ordinary work, because being wedged
is what stops work reaching it.

```
WEDGED --owner `kick`--> PROBING --one synthetic health task-->
    accepted and completed  -> HEALTHY
    failed or timed out     -> WEDGED
```

`PROBING` is a real state and is published: exactly one synthetic health task is
outstanding in it, it is not eligible for ordinary work, and the probe's timeout
is what bounds the state. The synthetic task takes a lease like any other task,
so a probe that dies leaves no residue.

**Reconciliation is the supervisor's, and it is a backstop, not the primary
path.** The fast path is event-driven: a task arrives, the supervisor routes and
offers it immediately; an executor event drives the next transition immediately.
The backstop runs on a fixed period and does four things, in this order:

1. Apply transition 8 to every non-terminal row whose `lease_until` has passed.
2. Complete any row whose result is already on disk under its canonical task ID —
   see the completion-before-release row of the failure matrix.
3. Re-route every `PENDING` row, applying the routing table.
4. Probe every worker's health and republish `state/pool-status.json`.

There is no per-executor reconciliation, and no executor re-lists the task
directory. One party looks at a task, decides its target, takes the lease
atomically, and notifies that target.

**The zero-candidate schedule is removed by construction, and reconciliation is
not what removes it.** A schedule in which every candidate declines is a property
of *distributed* suppression: two parties sample the same aging state
independently, each concludes the other is the target, each declines, and
declining leaves no file event for anything to react to. One party evaluating one
task once cannot produce that schedule, whatever it decides. So the periodic
backstop above exists for lease expiry and restart convergence only; it is not
repairing a hole in routing, and adding or removing it does not change the
routing table's outcome for any task.

## Failure/recovery matrix

Each crash window is a seam between two writes. Because control state is one
row, every window is a recognisable row state and the recovery is a transition,
not an inference from a filesystem.

| window | store state after the crash | what the supervisor does on restart |
|---|---|---|
| **offer-before-delivery** — the row was offered, the executor was never told | `OFFERED`, `lease_until` in the past, `accepted_at` NULL | transition 8 returns it to `PENDING`, `attempt` increments, the next pass re-offers. No duplicate is possible: nothing accepted it |
| **delivery-before-accept** — the executor was told, no accept was recorded | `OFFERED`, lease expired, `accepted_at` NULL | transition 8, then re-offer. A late accept from the old offer carries the old `attempt` and matches zero rows in transition 3, so it is refused rather than honoured |
| **accept-before-completion** — the executor accepted and died mid-run | `ACCEPTED` or `RUNNING`, lease expired | transition 8, then re-offer under `attempt + 1`. The dead executor's late completion matches zero rows in transitions 6 and 7. Side-effect idempotency for a re-run is the executor's, keyed on the canonical task ID; the store guarantees at most one live lease, not at-most-once side effects |
| **completion-before-release** — the work finished and the terminal write did not land | `RUNNING`, lease expired, and a result file exists under the canonical task ID | reconciliation step 2 runs **before** step 3: the supervisor applies transition 6 from the result on disk instead of re-offering. This is why the executor writes its result before reporting completion, and why the ordering of the two reconciliation steps is normative |

**Supervisor restart carries no in-memory state.** On start it opens the store,
runs the reconciliation pass above, and resumes. A row with a live lease is left
alone until that lease expires; that is the only thing a restart has to trust,
and it is a stored timestamp rather than a process it can no longer see.

**Executor restart carries none either.** An executor that restarts has accepted
nothing; it waits to be offered work. It does not scan for work to take.

## Pool membership and the pin table

Both are supervisor-owned, and both change only through owner commands.

**Membership** is the instance registry: a worker is registered with `role:
"worker"` and `pool: <name>` when it is created, and deregistered when it is
removed. There is no second file, no sentinel and no cached count — a membership
record that can disagree with the registry is a defect, not a mechanism.

**The pin table** is `state/pool/bindings.json`, one writer (the supervisor) and
one reader (the supervisor):

```json
{"version": 1,
 "bindings": {"<room-id>": {"instances": ["<name>", "..."], "pinned": true}}}
```

- **Write:** temp file plus `os.replace` in the same directory, never `>` and
  never read-modify-write. A reader inside a truncate window sees zero bytes and
  reads that as "no bindings".
- **Read:** every routing pass re-reads; there is no cached copy to invalidate.
- **Missing, unreadable or unparseable: fail toward the core agent.** The reader
  answers "not bound" and work continues. A binding exists to stop scatter, never
  to stop work.
- **Only `pinned: true` binds.** A bare entry without it does not constrain
  routing.
- **`instances` is an unordered membership set.** Position carries no meaning,
  there is no primary and no failover order. The lease decides which member takes
  which task. A room is unserved only when **every** member is ineligible, never
  when one is.
- The set is rewritten by exactly one event: an owner re-bind. A worker going
  dead rewrites nothing.

**The owner's commands.** Every pool command is an owner task carrying a
`pool_command` field and `requested_worker: core`, both written into the body the
server then stamps. `pool_command` carries the kind (`pin`, `unpin`, `rebind`,
`kick`, `run-on-core`, `create-worker`, `remove-worker`, `resize`, `set-model`,
`cancel`). `source` keeps its transport meaning and is not the discriminator.

An unverified envelope is never worker work and never mutates the pool:

| stamp verdict | `pool_command` honoured | `requested_worker` honoured | disposition |
|---|---|---|---|
| **verified** | yes | yes | the supervisor executes the command |
| **unsigned** (no stamper, or the stamper raised) | no | no | refused, quarantined with the reason, and the owner is told it did not run. Not retried silently |
| **invalid** (stamp present, MAC mismatch) | no | no | refused, quarantined as tamper, reported loudly |
| **unverifiable** (no local key, or a corrupt one) | no | no | refused and held, classified as a local outage: the host cannot judge envelopes at all until the key is restored |

**Identity: the canonical task ID is the only key.** It is the value of the task
file's `id:` header, resolved through the shared resolver
(`task_archive.task_id_for`), and it is the store's primary key, the lease key,
the result key and the offer key.

A filename is never that key. A task's on-disk filename is transient by design:
it is written as `task-<id>.txt`, may be renamed while in flight, and is archived
under the canonical name. **Measured 2026-09-07:** a classifier that keyed on
`path.stem` accumulated 254 store rows under names that no longer exist on disk,
including a non-numeric `claimed-core-legacy` suffix. Two rules follow and both
are normative: derive identity from the `id:` header and never from the
filename, and never assume a worker identifier is numeric.

## Staged PRs against main

Each merges before the next opens.

1. **This document.** Docs only; no code.
2. **Supervisor skeleton, the store, and the state machine.** The supervisor
   process and its supervision unit, the SQLite schema, and transitions 1 through
   9 with their tests. *Prerequisite:* none — it ships alongside today's path and
   routes nothing until step 4.
3. **The executor interface and the Claude adapter.** `offer` / `accept` /
   `start` / `cancel` / `events` / `health`, and the Claude implementation.
   *Prerequisite:* step 2, because an adapter with no store has nothing to report
   transitions into.
4. **Routing, the pin table, and the owner commands.** The routing table, the
   binding reader and writer, the `pool_command` envelope and its stamp matrix.
   *Prerequisite:* step 3, because a routing decision is only testable end to end
   once one adapter can accept an offer.
5. **The Codex App Server adapter.** Turn creation as delivery, turn lifecycle as
   accept and start. *Prerequisite:* step 3, whose interface it implements
   without changing.
6. **The ACP adapter.** `session/prompt` as delivery. *Prerequisite:* step 3, for
   the same reason as step 5.
7. **Reconciliation and health probing.** The periodic backstop, the four health
   states, and the kick/`PROBING` cycle. *Prerequisite:* step 4, because
   re-routing a reconciled row needs the routing table to re-route it with.
8. **Installer and packaging.** Supervision units for the supervisor and each
   worker, per-worker model capture, and the migration off the current pool.
   *Prerequisite:* step 7, because the installer's acceptance test is a full
   create-run-reconcile-remove cycle.

## Out of scope for v1

- **Auto-scale** in either direction. The supervisor reports saturation; it never
  resizes the pool.
- **Cloud or remote workers.** Every executor in v1 is local to the supervisor.
- **The stand card** and any pool dashboard beyond `state/pool-status.json` and
  what the app already renders from it.
- **Any second scheduler.** There is exactly one local pool supervisor per
  install, and no whole-queue policy process beyond it.
- **A scheduling loop inside a worker agent.** Worker agents run no proactive
  loop; a per-worker proactive loop is out of scope, and so is any pin
  enforcement that would live in one.
- **Sutando-side fan-out** of one task to N workers. Server-side fan-out — one
  envelope per addressed worker, each with its own canonical task ID — is in.
- **Group identity, group admission and group release.** The binding unit is the
  room and the concurrency unit is the task; a guarantee quantified over a set of
  rooms needs machinery v1 does not have.
- **Priority policy beyond a local sort.** Each routing pass orders its
  candidates `urgent > normal > low`, then oldest first.
