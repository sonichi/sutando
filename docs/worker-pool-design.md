# Worker pool — design (v1)

**Status:** normative design, owner-decided 2026-09-07. This is step 1 of staging
#3604 into PRs against `main`; #3604 stays open as a reference implementation and
is not merged as one piece. The record behind every decision here — what it
supersedes, what was rejected, and the measurements that forced each choice — is
[`worker-pool-design-notes.md`](worker-pool-design-notes.md), which is not
normative: where the two disagree, this file wins.

## Summary

A Sutando install runs one **pool supervisor**: a local process that holds no
LLM session and depends on none. It is the only scheduler. It owns routing, the
task lease, worker lifecycle, pool membership, the pin table, reconciliation of
expired leases, and status reporting.

Every agent session — the core agent and every worker agent — is an **executor
only**. An executor is offered a task, accepts it, runs it, and reports the
outcome. It does not route, does not assign, does not reclaim, and does not
decide who else may run.

All task control state lives in a **supervisor-owned durable task journal,
implemented as immutable task payloads plus atomically replaced per-task state
records**. The supervisor is its sole writer, so two parties cannot both believe
they own a task, and a crash between any two writes leaves a complete record the
supervisor can finish on restart.

The starting point is zero workers. A fresh install runs the supervisor and the
core agent and nothing else; every worker-shaped mechanism below is inert until
the owner's first create-worker command, and removing the last worker returns
the install to exactly that state.

## Components and ownership

| component | responsibilities | must not |
|---|---|---|
| **pool supervisor** | the only scheduler. Routes each task to exactly one target; opens, renews and expires the lease; drives worker lifecycle (create, start, shutdown, resume, kick); owns pool membership and the pin table; reconciles expired leases and re-offers; is the sole writer of `task-state/`, `bindings/` and `outbox/`, and writes `state/pool-status.json` | run an LLM session, or depend on one being responsive for any control action |
| **core agent** | an executor. Runs tasks for rooms with no binding, and every task the owner explicitly directs to it. Writes its own result file and its own receipts under `executor-events/core/` | write any authoritative state record, route, assign, lease, reclaim, sweep, or hold any pool control state |
| **worker agent** | an executor. Runs tasks bound to it or addressed to it. Writes its own result file and its own receipts under `executor-events/<executor>/` | write any authoritative state record, route, assign, lease, reclaim, or claim anything it was not offered |
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
exactly the outage it exists to act in. That is the principle already keeping a
resource-exhaustion report out of the exhausted session, applied one row further:
to the scheduler itself.

Saturation is something the supervisor **reports** in `state/pool-status.json`.
Creating a worker is a spend decision and requires an owner command.

## Task state machine

Task control state is a **supervisor-owned durable journal**. Each task has one
immutable payload file and one state directory, and the supervisor is the only
process that writes into that directory.

### Layout

```
workspace/
├── tasks/
│   └── task-123.txt                    # immutable task payload, never renamed
├── task-state/
│   └── task-123/
│       ├── state.json                  # the authoritative state
│       └── stale-results/              # refused late results, kept for audit
├── results/
│   └── task-123/
│       └── 3.txt                       # the result, keyed by lease generation
├── bindings/
│   └── rooms.json                      # room to executor, supervisor-written
├── workers/
│   ├── core.json
│   └── worker-1.json
├── outbox/
│   └── task-123.json                   # durable delivery record
├── executor-events/
│   └── worker-1/
│       └── completion-assign-abc.json  # an unconsumed receipt
└── run/
    ├── pool-supervisor.sock            # the socket executors report over
    └── pool-supervisor.lock            # advisory single-instance lock
```

**The state record** is the whole of a task's control state:

```json
{"version": 7, "task_id": "task-123", "state": "RUNNING", "room_id": "!room:ag2.space",
 "executor_id": "worker-1", "assignment_id": "assign-abc", "lease_generation": 3,
 "offer_expires_at": null, "lease_until": "2026-09-08T05:30:00Z",
 "updated_at": "2026-09-08T05:25:00Z"}
```

States: `PENDING` → `OFFERED` → `ACCEPTED` → `RUNNING` → `SUCCEEDED` | `FAILED`.
`SUCCEEDED` and `FAILED` are terminal, `PENDING` is the only re-offerable state,
and `version` increments on every write and names the record a reader saw.

**`state.json` is never overwritten in place.** One write is: serialise the whole
record, write `state.json.tmp-<unique>` in the same directory, `fsync` the temp
file, `os.replace()` it onto `state.json`, then `fsync` the parent directory. A
reader sees the complete old record or the complete new one, and never a torn
one. Only the supervisor writes `state.json`, so there is no multi-writer
contention at the file layer and no lock is taken around a record.

**Every transition is one such replacement, guarded by a generation check:** an
event applies only when its `assignment_id` and `lease_generation` equal the
record's current values. An event that fails the check did not happen, and the
reporter is answered with that fact instead of retrying blind.

| # | transition | performed by | journal write |
|---|---|---|---|
| 1 | ingest → `PENDING` | supervisor | create `task-state/<task-id>/` and write `state.json` at `version: 1`, `lease_generation: 0`, `executor_id: null`. An existing directory is left untouched |
| 2 | `PENDING` → `OFFERED` | supervisor | increment `lease_generation`, mint a fresh `assignment_id`, set `executor_id` and `offer_expires_at` |
| 3 | `OFFERED` → `ACCEPTED` | supervisor, on the executor's accept event | set `state`, set `lease_until`, clear `offer_expires_at` |
| 4 | `ACCEPTED` → `RUNNING` | supervisor, on the executor's start event | set `state` and extend `lease_until` |
| 5 | lease renewal | supervisor, on the executor's heartbeat event | extend `lease_until` alone |
| 6 | `RUNNING` → `SUCCEEDED` | supervisor, on the executor's completion event | set `state`, clear `executor_id`, `assignment_id` and `lease_until`, then write the outbox record |
| 7 | `RUNNING` → `FAILED` | supervisor, on the executor's failure event | row 6's write with `state` set to `FAILED` |
| 8 | lease expiry → `PENDING` | supervisor reconciliation | increment `lease_generation`, clear `executor_id`, `assignment_id`, `lease_until` and `offer_expires_at` |
| 9 | owner cancel → `FAILED` | supervisor, on a `pool_command` task | row 7's write from any non-terminal state, exempt from the generation check because the owner holds no lease |

**`lease_generation` is the anti-replay token.** An offer carries the generation
it was made under and the `assignment_id` minted with it, and the executor echoes
both in every event. Rows 3 through 7 apply only on a match, so an event produced
under a generation the supervisor has already expired (row 8 increments it) is
refused. Without it, a late accept from an executor that was slow rather than
dead would take a lease the supervisor has already re-offered.

**`executor_id` is possession, not the room's binding.** Rows 3 through 7 match
on it too, so a report from any other executor about that task is refused. The
pin table records intent and is what the status surface reads; `executor_id`
records possession and is what the check guards alongside the generation.

**A non-terminal record always carries a `lease_until` in the future or is
reconcilable.** There is no state in which a task is owned by nobody and also not
re-offerable — `PENDING` has no lease and is re-offerable, and every other
non-terminal state carries a lease that either renews or expires into row 8. That
is the invariant the backstop enforces and the transitions test pins.

**Task payloads are not in the journal.** `tasks/<task-id>.txt` holds the body,
is written once, and is never renamed; the journal holds control state only, and
no task body is parsed for routing.

### One authoritative source per fact

| fact | sole source |
|---|---|
| task state | `state.json` field `state` |
| current executor | `state.json` field `executor_id` |
| current lease | `state.json` field `lease_generation`, with `lease_until` |
| room binding | `bindings/rooms.json` |
| result produced | `state` reads `SUCCEEDED` |
| delivered | the outbox record |

One fact is never expressed through two mechanisms — not a running marker file
existing, not a claim file existing, and not a `claimed` suffix in a filename.

### Executor reports: a socket, with a receipt behind it

Executors never write authoritative state. An executor reports each event to the
supervisor over the Unix domain socket `run/pool-supervisor.sock`:

```json
{"type": "accept", "task_id": "task-123", "assignment_id": "assign-abc", "lease_generation": 3}
{"type": "complete", "task_id": "task-123", "assignment_id": "assign-abc",
 "lease_generation": 3, "result_path": "results/task-123/3.txt"}
```

The supervisor runs the generation check and then replaces `state.json`. A claim
file paired with an accept file is not needed: an accept is a journal write
rather than a file whose presence has to be interpreted.

**When the supervisor is unreachable, the executor's write order is normative.**
On completion it writes its result to `results/<task-id>/<generation>.txt` by the
same atomic replacement, then a receipt to
`executor-events/<executor>/completion-<assignment-id>.json`, then attempts the
socket report. An unreachable socket leaves the receipt on disk and the executor
stops there. A receipt is an unconsumed inbox message, not a second copy of task
state: `state.json` is the state, `executor-events/` is the inbox, and the two do
not overlap. **Supervisor restart** loads every `task-state/*/state.json`,
validates each receipt's `assignment_id` and `lease_generation`, applies the ones
that pass, writes their outbox records, and deletes every receipt it consumed,
then runs the reconciliation pass under **Health model and reconciliation**.

**A stale generation is refused, never overwritten.** If an executor completes
generation 2 while the supervisor has already re-offered the task as generation 3
to another executor, that completion fails the check. It is refused as the
canonical result and may be kept for audit at
`task-state/<task-id>/stale-results/generation-<n>-<executor>.txt`; the current
result is not touched and no stale event reaches `state.json`.

**Single instance.** `run/pool-supervisor.lock` carries an OS advisory lock —
`fcntl.flock` with `LOCK_EX | LOCK_NB` in Python, `fs2::FileExt::try_lock_exclusive`
in Rust — held for the process's lifetime, and a second supervisor fails to take
it and exits. A process manager may enforce one instance too, with the lock as
defence in depth. A PID file alone is not the guard, because a PID is reused and
the gap between reading one and acting on it is a race.

**Costs accepted,** all four affordable at Sutando's scale of a few workers, tens
of active rooms and a handful of pending tasks. A scan of `task-state/` is the
only enumeration, so there is no efficient query over tens of thousands of
pending tasks. There is no cross-object transaction, so a room binding and a task
assignment cannot commit together — each is its own atomic replacement, ordered by
the supervisor. Statistics need a full scan or a separate append-only log, and
schema changes are hand-designed and hand-migrated.

### The journal in one table

| item | this design |
|---|---|
| task payload | `tasks/<task-id>.txt`, stable and immutable |
| task state | `task-state/<task-id>/state.json`, atomically replaced by the supervisor |
| executor reports | a socket notification with a durable receipt behind it |
| result | `results/<task-id>/<generation>.txt` |
| delivery | a separate durable outbox |
| recovery | on start the supervisor scans non-terminal records and unconsumed receipts |

## Routing table

The supervisor makes one routing decision per task, once, and takes the lease in
the same pass. There is no second party evaluating the same question, so no
schedule exists in which two candidates both decline.

**Message text never participates in routing.** Routing reads the envelope's
verified fields and the pin table and nothing else, so a room message whose body
says "send this to worker-2" is an ordinary task.

| condition | target | journal effect |
|---|---|---|
| `requested_worker` names a worker, and that worker is `HEALTHY` | that worker | row 2 with `executor_id` = that worker |
| `requested_worker` names a worker that is not `HEALTHY` | none | the record stays `PENDING`; the room is reported as waiting |
| `requested_worker` names the core (every `pool_command` envelope does) | the core agent | row 2 with `executor_id` = `core` |
| no `requested_worker`, the room is bound in the pin table, and its bound worker is `HEALTHY` | that worker | row 2 with `executor_id` = the bound worker |
| no `requested_worker`, the room is bound, and no bound worker is `HEALTHY` | none | the record stays `PENDING`; the room is reported as waiting |
| no `requested_worker`, the room is **unbound** (the pin table names no worker for it) | the core agent | row 2 with `executor_id` = `core` |
| the pin table is missing, unreadable, or unparseable | the core agent | the reader answers "not bound", so work continues |

`requested_worker` overrides an ordinary pin: it is the server's decision, written
at ingress into the envelope the server then stamps, and the bridge records it
verbatim. A sender-controlled routing header is not honoured, and an unverified
envelope is never worker work — see **Pool membership and the pin table**.

When more than one target could take one task — a room bound to a set of
workers, or a re-offer racing a late accept — the **lease decides**. Row 2 is
applied once, under one generation; the loser fails the generation check and
takes no action.

**A bound room whose workers are all unavailable stays `PENDING`, and that is a
product decision, not a failure.** Nothing else takes the work: no other worker,
and not the core agent. Because the wait is deliberate it must be visible, so the
supervisor reports the room in `state/pool-status.json` and the app renders it as:

> **Room is waiting for worker-2** — Rebind / Restart / Process with core

Those three are owner commands, and each is an ordinary task carrying
`pool_command`:

| choice | `pool_command` | what the supervisor does |
|---|---|---|
| **Rebind** | `pin` | rewrites the room's entry in the pin table, then re-routes its `PENDING` records on the next pass |
| **Restart** | `kick` | drives the worker's health transition (see **Health model and reconciliation**); the room's records stay `PENDING` until the worker reaches `HEALTHY` |
| **Process with core** | `run-on-core` | routes that room's `PENDING` records to the core agent for this occurrence, leaving the binding intact |

Without an owner choice the records stay `PENDING` indefinitely, which is visible
and recoverable. Routing them somewhere the owner did not ask for is neither.

## Executor interface and runtime adapters

One interface, implemented once per runtime — `Executor.offer(task)`,
`accept(task)`, `start(task)`, `cancel(task)`, `events()`, `health()`. The
supervisor speaks only this; runtime differences live below it.

| call | direction | contract |
|---|---|---|
| `offer(task)` | supervisor → adapter | deliver the offer, carrying the canonical task ID, the payload's path, the `assignment_id` and the `lease_generation`. Returns only whether delivery was attempted; it is not an accept |
| `accept(task)` | adapter → supervisor | the executor has taken the task. Echoes the canonical task ID, the `assignment_id` and the `lease_generation`. Drives row 3 |
| `start(task)` | adapter → supervisor | execution has begun. Drives row 4 |
| `cancel(task)` | supervisor → adapter | stop work on this task. Best-effort; the supervisor does not wait on it before row 9 |
| `events()` | adapter → supervisor | the stream carrying start, heartbeat, completion and failure. Drives rows 4 through 7 |
| `health()` | supervisor → adapter | answers the executor's health state for the model below. Must not require a model call to answer `UNAVAILABLE` |

**An executor must explicitly accept.** Delivery is not admission: an offer that
is delivered and never accepted expires with its lease and is re-offered. That
removes the delivery deadlock in which an executor is told about work it is
structurally unable to take.

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
| **shutdown** | `remove-worker` / `resize` down | in this order: stop the process; expire or cancel the leases it holds so their records return to `PENDING`; remove the worker from every room's binding; deregister it | records re-offerable; rooms left with no binding are unbound and route to the core agent |
| **resume** | host wake, or `resume` | waits for a healthy probe before offering. A host sleep is not N dead workers | worker `UNAVAILABLE` → `HEALTHY` |
| **kick** | `kick` (the status surface's **Restart**) | `WEDGED` → `PROBING`, then one synthetic health task | see the health model below |

**Shutdown order is the contract.** Stopping the process first is what makes the
lease expiry safe: expiring a lease a live worker still holds would let the
re-offer and the original worker run the same task. Rewriting the bindings first
would let the worker act on a binding that no longer names it.

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
returns to `UNAVAILABLE` and the ordinary probe decides. Failing toward re-probing
is deliberate: a worker stranded by a record nobody clears is invisible, because
it is up and simply never runs anything.

**The kick cycle is the only exit from `WEDGED`, and it is commanded.** A wedged
worker cannot demonstrate recovery through ordinary work, because being wedged is
what stops work reaching it.

```
WEDGED --owner `kick`--> PROBING --one synthetic health task-->
    accepted and completed  -> HEALTHY
    failed or timed out     -> WEDGED
```

`PROBING` is a real state and is published: exactly one synthetic health task is
outstanding in it, it is not eligible for ordinary work, and the probe's timeout
bounds the state. The synthetic task takes a lease like any other, so a probe
that dies leaves no residue.

**Reconciliation is the supervisor's, and it is a backstop, not the primary
path.** The fast path is event-driven: a task arrives and is routed and offered
at once, and an executor event on the socket drives the next transition at once.
The backstop runs on a fixed period and does four things, in this order:

1. Consume every receipt under `executor-events/`, applying the ones that pass
   the generation check and archiving the rest under `stale-results/`.
2. Apply row 8 to every non-terminal record whose `lease_until` has passed.
   **Step 1 runs before this one, and that order is normative** — see the
   completion-before-release row of the failure matrix.
3. Re-route every `PENDING` record, applying the routing table.
4. Probe every worker's health and republish `state/pool-status.json`.

There is no per-executor reconciliation, and no executor re-lists the task
directory.

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

Each crash window is a seam between two writes. Because a task's control state is
one atomically replaced record, every window is a recognisable record state plus a
receipt outcome, and its recovery is a transition rather than an inference.

| window | record after the crash | what the supervisor does on restart |
|---|---|---|
| **offer-before-delivery** — the record was offered, the executor was never told | `OFFERED` at generation N, `offer_expires_at` past, no receipt | row 8 returns it to `PENDING` at generation N+1 and the next pass re-offers. No duplicate is possible: nothing accepted it |
| **delivery-before-accept** — the executor was told, no accept was recorded | `OFFERED` at generation N, the offer expired, no receipt | row 8, then re-offer. A late accept still carrying generation N fails the check, so it is refused rather than honoured |
| **accept-before-completion** — the executor accepted and died mid-run | `ACCEPTED` or `RUNNING` at generation N, lease expired, no receipt | row 8, then re-offer at N+1. The dead executor's late completion fails the check. Side-effect idempotency for a re-run is the executor's, keyed on the canonical task ID; the journal guarantees at most one live lease, not at-most-once side effects |
| **completion-before-release** — the work finished and the terminal write did not land | `RUNNING` at generation N, lease expired, and a receipt for generation N unconsumed | reconciliation step 1 runs **before** step 2: the receipt passes the check and is applied as row 6, so the record never reaches expiry and the finished task is not re-offered. This is why the executor writes its result, then its receipt, then reports |
| **supervisor-down** — the supervisor was absent while executors finished | any state; one or more receipts unconsumed | the same pass, over every receipt that accumulated. An executor that cannot reach the socket keeps its receipt and writes nothing into the journal |
| **stale-generation completion** — a superseded executor reports late | the record already at generation N+1 under another executor | the receipt fails the check, is archived to `stale-results/generation-<n>-<executor>.txt`, and is deleted from the inbox. The current result is never overwritten |

**Supervisor restart carries no in-memory state.** On start it loads every
`task-state/*/state.json`, runs the reconciliation pass above, and resumes. A
record with a live lease is left alone until that lease expires; that is the only
thing a restart trusts, and it is a stored timestamp rather than a live process.

**Executor restart carries none either.** An executor that restarts has accepted
nothing and waits to be offered work; it does not scan for work to take.

## Pool membership and the pin table

Both are supervisor-owned, and both change only through owner commands.

**Membership** is the instance registry: a worker is registered with `role:
"worker"` and `pool: <name>` when created, and deregistered when removed. There is
no second file, no sentinel and no cached count — a membership record that can
disagree with the registry is a defect, not a mechanism.

**The pin table** is `bindings/rooms.json`, one writer (the supervisor) and one
reader (the supervisor):

```json
{"version": 12,
 "bindings": {"!room-a:ag2.space": {"instances": ["worker-1"], "pinned": true,
                                    "generation": 4}}}
```

- **Write:** the same atomic replacement as a state record, never `>` and never
  read-modify-write. `version` increments on every write.
- **Read:** every routing pass re-reads; there is no cached copy to invalidate.
- **Missing, unreadable or unparseable: fail toward the core agent.** The reader
  answers "not bound" and work continues. A binding exists to stop scatter, never
  to stop work.
- **Only `pinned: true` binds.** A bare entry without it does not constrain
  routing.
- **`instances` is an unordered membership set.** Position carries no meaning and
  there is no primary and no failover order. The lease decides which member takes
  which task, and a room is unserved only when **every** member is ineligible.
- The set is rewritten by exactly one event: an owner re-bind. A worker going
  dead rewrites nothing.

**The owner's commands.** Every pool command is an owner task carrying a
`pool_command` field and `requested_worker: core`, both written into the body the
server then stamps. `pool_command` carries the kind (`pin`, `unpin`, `rebind`,
`kick`, `run-on-core`, `create-worker`, `remove-worker`, `resize`, `set-model`,
`cancel`); `source` keeps its transport meaning and is not the discriminator.

An unverified envelope is never worker work and never mutates the pool:

| stamp verdict | `pool_command` honoured | `requested_worker` honoured | disposition |
|---|---|---|---|
| **verified** | yes | yes | the supervisor executes the command |
| **unsigned** (no stamper, or the stamper raised) | no | no | refused, quarantined with the reason, and the owner is told it did not run. Not retried silently |
| **invalid** (stamp present, MAC mismatch) | no | no | refused, quarantined as tamper, reported loudly |
| **unverifiable** (no local key, or a corrupt one) | no | no | refused and held, classified as a local outage: the host cannot judge envelopes at all until the key is restored |

**Identity: the canonical task ID is the only key.** It is the value of the task
file's `id:` header, resolved through the shared resolver
(`task_archive.task_id_for`), and it names the `task-state/` directory, the lease,
the result and the offer.

A filename is never that key. **Measured 2026-09-07:** a classifier that keyed on
`path.stem` accumulated 254 records under names that no longer exist on disk,
including a non-numeric `claimed-core-legacy` suffix. Three rules follow and all
are normative: derive identity from the `id:` header and never from the filename,
never assume a worker identifier is numeric, and never rename
`tasks/<task-id>.txt`, so that no suffix and no rename can express a state.

## Staged PRs against main

Each merges before the next opens.

1. **This document.** Docs only; no code.
2. **Supervisor skeleton, the task-state journal, the state machine, and the
   lock.** Its supervision unit, the `task-state/` journal with its atomic
   replacement, rows 1 through 9 with their tests, and the single-instance lock.
   *Prerequisite:* none — it ships alongside today's path and routes nothing
   until step 4.
3. **The executor interface and the Claude adapter.** `offer` / `accept` /
   `start` / `cancel` / `events` / `health`, the socket and the receipt inbox,
   and the Claude implementation. *Prerequisite:* step 2, because an adapter with
   no journal has nothing to report into.
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
   re-routing a reconciled record needs the routing table to re-route it with.
8. **Installer and packaging.** Supervision units for the supervisor and each
   worker, per-worker model capture, and the migration off the current pool.
   *Prerequisite:* step 7, because the installer's acceptance test is a full
   create-run-reconcile-remove cycle.

## Out of scope for v1

- **A database-backed store is not used.** The journal
  under **Task state machine** is the store.
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
