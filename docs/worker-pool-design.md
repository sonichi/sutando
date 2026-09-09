# Worker pool — design (v1)

**Status:** normative, owner-decided 2026-09-08/09. Step 1 of staging #3604 into
PRs against `main`; #3604 stays open as reference and is not merged as one piece.
Carries the requirements of #3860 and the protocol of #3314. The record behind each
decision is [`worker-pool-design-notes.md`](worker-pool-design-notes.md), which is
not normative: where the two disagree, this file wins.

## What this is for

Today one Sutando is one assistant with one queue. That holds until two lines of
work must be live at once, and until the owner wants "give this to the one handling
support" to mean something.

**A worker is a second named assistant inside the same install** — same owner,
connectors and authorisation; its own inbox, context and name. Three requirements:
**durable identity** (the owner keeps finding the same worker across restarts and
replaced sessions), **human control** (creation, targets, bindings, permissions,
pause and resume are the owner's, and the system carries them out), and
**steering** (correcting or cancelling work already running).

Not a way to go faster — sub-agents already give concurrency, but no name, no
bindings, no permissions, no existence between tasks. Not a second Sutando — that
is a second owner relationship and a second copy of every connector, and it is the
right answer only when a second *identity* is what you want.

## Vocabulary

**core** (the one session every install has), **worker** (a named task executor the
core created), **router** (turns a declared target into a delivery), **Task Bridge**
(the single admission gate), **process supervisor** (`launchd` on macOS), **pool**
(core + router + workers). "Lead" and "follower" are retired.

A worker runs on whatever agent runtime the install uses — Claude Code today, whose
unit of execution is a *session*. That is the runtime's concept, not Sutando's: a
worker is not a session, has many over its life, and exists with none running.

**A worker has an id and a label.** The id is the worker's identity, opaque, never
changed or reused; every path, filename, roster key and header names it. The label
is the owner's display name — mutable, one roster field, never in a path or a glob.
Renaming is a one-field edit. Labels resolve to ids where intent is captured, never
at routing.

## Task, delivery, attempt

| level | what it is | cardinality |
|---|---|---|
| task | the immutable payload | one |
| delivery | one task, one intended recipient | one per recipient |
| execution attempt | which incarnation claimed it, and its progress | one or more |

```
tasks/task-123.json                    the task: immutable, never copied
deliveries/worker-2/task-123           a sentinel — existing IS the assignment
deliveries/worker-2/task-123.claimed   the same sentinel, suffix substituted
(sentinel removed, payload archived)   finish
```

**Files under `deliveries/` are sentinels, not tasks.** A sentinel needs no content:
the id is its name, the recipient is its folder. Five recipients is one payload and
five small files, which cannot drift because only one payload exists.

**Creating the delivery assigns; renaming it claims.** The rename moved rather than
disappeared: `os.rename` is atomic and exclusive, and a record that merely says
"addressed to you" is not — two live incarnations of one worker would both run it.

**A folder per recipient** is not required for correctness (filtering yields the
same set, and neither layout enforces anything while all workers share one OS
identity). It is chosen so each watcher wakes only for its own work, pending work
is a directory listing, recovery is bounded by one worker, a wrong glob finds an
empty directory, and a permission boundary becomes possible without a migration.

It adds two failure modes, named not solved: an **orphan delivery** (sentinel whose
payload was archived) and a **vanishing payload** (payload archived between reading
the sentinel and reading it).

## Request, assignment, claim

| state | expressed as | owner |
|---|---|---|
| request | file content: `requested_worker: worker-2` | producer or bridge |
| assignment | a delivery record in the recipient's folder | the router alone |
| claim | that record renamed | that worker alone |

The canonical id never changes; suffixes substitute, never append; consumers key on
the `id:` header.

## Who does what

| role | owns — conceptually | runs in — v1 |
|---|---|---|
| owner | intent and authorisation. Other authorised tiers exist, granted by the owner, never exceeding them | — |
| core | everything it does with no pool. Plus: default target, pool lifecycle, model choice, all spend, compiles the roster, **owns worker recovery** | its own session |
| Task Bridge | admits every new task: attests the submitter, validates authorisation | ahead of the queue |
| router | resolve one task against the roster, write one delivery per declared recipient, report status | the task watcher — **no daemon of its own** |
| worker | claim and execute deliveries in its own folder; submit requests in scope; run crons of its own | its own session |
| recipient's watcher | the receiving half of a worker or the core — part of it, separate from its session | `watch-tasks-stream.sh`, one per recipient |
| process supervisor | starts and restarts the core and every watcher. **Never a worker's session** | `launchd` on macOS |

A row is a boundary, not a process; several share one.

### The nevers

A worker never selects its own work, writes or alters a delivery, or reads another
recipient's folder. The router never infers intent, substitutes a target, or keeps
a process alive. The core never routes once a router exists. The process supervisor
never supervises a worker's session — a restart is not a resume, and deciding what
a stopped worker needs is judgement. An envelope stamp never authorises. A stale
beat never authorises release.

**One invariant is positive: every task worker is associated with a task delivery
mechanism.** Without one it cannot receive work. In Sutando that mechanism is a
watcher; creating a worker creates it, removing one removes it. The core is a
recipient with a watcher like any other, which is why it needs no second intake.

## Admission

**Every new task passes the Task Bridge** — no shortcut for the core, a timer, a
worker or a retry. **Authorisation is resolved, not read off the message:** a stamp
answers whether bytes changed, never whether an actor may act, so every pool command
needs an independently resolved owner capability. Permissions never escalate in
transit.

## Routing

The router's whole input is **the roster and the task**, which makes it testable by
replay. The core compiles the roster from the owner's declarations; the router only
reads it. Compilation has two branches: a declared target, or the core. A missing
roster is a refusal, never a default.

| the task declares | the router does |
|---|---|
| one target, alive | writes a delivery to that worker |
| a target set | one delivery to every member; selects no subset |
| nothing | writes to the core |
| a named target unavailable | holds and reports; substitutes no one |
| a target that does not exist | fails with a named error |

No `target_worker` or `fan_out` headers — a sender's message is not a routing
instruction. **Bindings hold until the owner changes them**; nothing is learned or
decayed, which is why no affinity table exists.

## Where the router runs

A role with a contract, not a process count — perhaps fifty lines of deterministic
code, running as a handler in the task watcher. One process, two watches: the
handler observes `tasks/`, the core *as a recipient* observes `deliveries/core/`.

**Two things are called "the core", and only one is the risk.** The watcher is
deterministic code; the session is an LLM that stalls and runs out of quota.
Routing in the watcher survives that. It does not survive the session exiting, and
it is exposed to **backpressure** — a reader that stops draining blocks the writer,
and any sweep sharing that path stops too.

**Reuse, not reinvention.** A separate router would re-watch `tasks/`, which the
core's watcher already does across 700 lines of incident-hardened code. The
coupling is one line, `printf 'TASK_FILE: …' || exit 0`; writing a file instead
removes the need for a live reader, and only then can the supervisor own it.

**But a file wakes nobody.** Two halves are needed: a **supervised watcher** that
persists deliveries without a live session, and a **recipient's watcher** that finds
pending deliveries and notifies its recipient, including after a restart. Its
notices are recoverable from durable state, and **a notice is not proof of
acceptance**.

**The core never switches intake protocols** — it reads `deliveries/core/` always,
and the worker count changes only what the handler decides. And "untouched" must be
claimed honestly: moving supervision and moving delivery from a pipe to a file *are*
changes to the single-core path, so if today's behaviour must be preserved for the
first pool release, they stay out of its default path.

## Completion

Fixed order: result, then done-flag, then archive. Residue is then unambiguous —
**result with no flag** means completed, never re-run; **claim with no result** means
died mid-work, released to the same worker.

`finish` is the single completion path and refuses unless the caller holds the
claim, the first body line echoes `task: <id>`, and the body is non-empty. A refusal
writes nothing. The echo exists because a worker holding two claims once wrote each
reply into the other's result file, and an owner's answer reached the wrong room.
Also: per-worker namespaced state, `.tmp-<worker>` staging, no-clobber archive.

## Worker states and recovery

| state | set by | the router may |
|---|---|---|
| `live` | the worker's beat | write deliveries to it |
| `recovering` | the core | hold; write nothing new |
| `abandoned` | the core | release its claims back to it |
| `retired` | the core | nothing |

**Stale is not dead.** A beat is an mtime, 30 s, considered stale at 90 s, and a
future-dated beat counts as stale too. A host sleep expires every beat at once. That
is why a release is not authorised by staleness: it keys on `abandoned`.

**Recovery is narrower than a sweep.** A worker reads its own folder at boot, and a
claim with no result releases to that same worker — the only party allowed to take
it. The ordinary case resolves itself with nobody sweeping. What remains for the
core is work belonging to a worker that will not return, which ends in a question to
the owner, and reporting so a long-claimed delivery is visible.

## Supervision

A periodic OS timer — five minutes, not a session cron — samples **work, not
processes**, writes a deduplicated anomaly record, and routes it to a pre-authorised
remedy or to the core for diagnosis.

| signal | evidence | authorises |
|---|---|---|
| process death | beat expired, session gone, not owner-paused, sustained | a pre-authorised restart |
| task stalled | unfinished work whose progress has not advanced | diagnosis only |

**Sub-agent activity counts as progress**, or the detector escalates the busiest
workers. Owner-paused outranks every signal. Detection and the pre-authorised remedy
are deterministic code, so they work while the core is stalled or out of quota.

## Crons, delegation, peer contact

**A worker may run crons of its own.** It may not register the *host* cron set (×N),
and its cron's output passes admission as a **request** — a timer is a trigger, not
an authority. Open: durability (a schedule in a session dies with it) and pause.

**Delegation is allowed, owner-initiated, non-compounding.** A worker may submit a
request naming another; never a delivery. A→B does not imply B→C; co-binding grants
nothing. Two budgets, depth and volume — depth 3 with fan-out 5 is 125 well-formed
tasks inside any single cap. Enforced at the Task Bridge. Unrelated to sub-agent
delegation inside a worker's own session.

**No worker↔worker channel, but a path — the front door.** A worker submits a
request; the recipient gets an ordinary delivery and cannot tell submitters apart.
One honest limit: two live sessions on a machine can reach each other through the
*runtime's* inter-session messaging. That carries no envelope, no attested submitter
and no record, so it cannot place work — a debugging affordance, not a path.

## Sets

A declared set delivers to **every** member: one delivery each, ids from
`(parent_id, worker_id)`, member list persisted so a restart finishes minting. First
answer cancels nothing; results are kept and grouped under the parent. Stable ids
prevent duplicate tasks, not duplicate effects.

## Phases and staging

**Phase 1 — delivery.** The router exists from the first install; it already has a
branch for "nothing declared, so the core runs it". Creating worker 1 adds a target,
not a component. Bindings, one durable addressee per line of work, at-most-once
completion, results delivered back.

**Phase 2 — recovery and policy.** Supervision, stranded work, sets, and fallback
policy for an unavailable target — data the owner declares, not a router judgement.
There is **no leaderless mode** — a worker does not claim unassigned work, ever.

Two separately reviewable stages: **first** improve the single-core delivery
mechanism alone (reuse the hardened code, add reconciliation, independent
supervision and durable handoff if needed), verified against session exit, watcher
restart, a missed notification and a stalled consumer; **then** add routing on top.
Sequenced this way the pool release changes routing decisions and nothing underneath
them.

## Implementation

Everything below is normative and meant to be built from directly.

### Layout and formats

```
<workspace>/
  tasks/<task-id>.json                    payload, immutable after admission
  tasks/archive/<task-id>.json            terminal
  deliveries/<recipient-id>/<task-id>          sentinel, 0 bytes, unclaimed
  deliveries/<recipient-id>/<task-id>.claimed  sentinel, 0 bytes, claimed
  results/<task-id>.txt                   reply body
  state/roster.json                       compiled; router reads only
  state/bindings.json                     owner-authored declarations
  state/pool-status.json                  owner-facing, atomically replaced
  state/workers/<id>.alive                worker beat (mtime only)
  state/watchers/<id>.alive               watcher beat (mtime only)
  state/workers/<id>/done/<task-id>.flag  done-flag
```

`<recipient-id>` is `core` or a worker id. Recipient ids match `[a-z0-9][a-z0-9-]{0,31}`;
a label never appears in a path.

**Payload** — written once by the Task Bridge:

```json
{"id":"task-123","created_at":"<RFC3339>","source":"discord|slack|room|cli|timer",
 "channel_id":"<opaque>","priority":"urgent|normal|low",
 "requested_worker":"worker-2|null","submitter":{"actor":"<id>","tier":"owner|team|…"},
 "authorisation":{"capabilities":["…"],"resolved_by":"task-bridge"},
 "body":"<text>","parent_id":"<task-id>|null"}
```

**Roster** — compiled by the core on any change to workers, bindings, states or scopes:

```json
{"version":41,"compiled_at":"<RFC3339>",
 "workers":{"worker-2":{"label":"support","state":"live","model":"…","scopes":["…"]}},
 "bindings":{"room:!abc:ag2.space":"worker-2","room:!def:ag2.space":["worker-2","worker-3"]}}
```

An assignment records the roster `version` it was made against.

### Router pass

Input is the roster and one admitted task; nothing else may be read.

1. Load `state/roster.json`. **Unreadable or absent → refuse the pass and report.** Never default to the core.
2. Resolve the target: `requested_worker` if present and non-null, else the binding for the task's source, else `core`. A set resolves to its member list.
3. Validate every target exists in the roster. Unknown → fail the task with a named error.
4. For each target whose state is `live`: if `deliveries/<target>/<task-id>` **or** `<task-id>.claimed` already exists, the task is delivered — do nothing. Otherwise `os.open(…, O_CREAT|O_EXCL)`, treating `EEXIST` as success. **Checking only the unclaimed name would recreate a sentinel for work already in flight and deliver it twice.**
5. For any target not `live`: write nothing, leave the task pending, and record it in `pool-status.json`. Never substitute.
6. For a set, ids are `<parent-id>-<target>`; persist the resolved member list on the parent so a restart finishes minting.

Order candidates `urgent > normal > low`, then oldest payload `created_at` first.

### Worker loop

1. Watch `deliveries/<me>/`, plus one sweep of the same folder at boot. The sweep is not optional: a delivery written while the session was down produces no event.
2. Candidates are entries with no `.claimed` suffix.
3. Claim: `os.rename(<task-id>, <task-id>.claimed)`. **`OSError` means somebody won the race — skip the task and continue.** It is the router releasing, or another incarnation of this same worker.
4. Read `tasks/<task-id>.json`. **Missing → the payload was archived under it; remove the stale sentinel and continue.**
5. Execute, then `finish`.

A worker reads no directory but its own, and writes no sentinel anywhere.

### finish — the single completion path

Refuse, writing nothing, unless all three hold: the caller holds `deliveries/<me>/<task-id>.claimed`; the body's first line is exactly `task: <task-id>`; the body after that line is non-empty.

Then, in this order, each step durable before the next:

1. `results/<task-id>.txt` — write to `results/.tmp-<me>-<task-id>`, `fsync`, `os.rename` into place.
2. `state/workers/<me>/done/<task-id>.flag` — create, `fsync`.
3. Remove the sentinel, then `os.rename` the payload into `tasks/archive/`.

Archiving is no-clobber: on collision mint `<task-id>.json.1`, `.2`, …

### Residue, read at boot

| on disk | means | do |
|---|---|---|
| result, no flag | completed, flag write was interrupted | write the flag, finish the archive; never re-run |
| `.claimed`, no result | died mid-work | rename back to `<task-id>`; the same worker retakes it |
| sentinel, no payload | payload already archived | remove the sentinel |
| payload, no sentinel, not archived | never routed | leave it; the router will place it |

### Supervision timer

An OS timer, 300 s, independent of any agent session.

1. For each worker in the roster that is not `retired` or owner-paused, collect: beat age, whether a session is running, count of `.claimed` sentinels, and the newest `mtime` among its done-flags and results.
2. **Process death** — beat older than 90 s *and* no session *and* not paused, on three consecutive ticks → the pre-authorised remedy: restart that worker's process. Deterministic; needs no core.
3. **Task stalled** — holds `.claimed` sentinels whose age exceeds a threshold while no done-flag or result has appeared → write an anomaly record and route it to the core for diagnosis. **Never authorises a restart.**
4. Anomaly records are deduplicated on `(worker-id, signal)` and cleared when work advances.
5. Verification is that work advanced, not that a process returned.

Sub-agent activity counts as progress. A future-dated beat counts as stale.

### Stage 1 — single-core delivery, no routing

Scope: keep one recipient, `core`. Move its intake to `deliveries/core/`, give the watcher a supervision unit, add the boot sweep and residue rules.

Acceptance, all four required:
- **Session exit** — kill the session mid-task; on restart, pending deliveries are announced and the in-flight one is retaken.
- **Watcher restart** — kill the watcher; deliveries written while it was down are announced on its return.
- **Missed notification** — write a delivery with no watcher running; it is found by the boot sweep.
- **Stalled consumer** — stop draining; the watcher must not block or lose an event.

Plus a **parity test**: one task end to end through a zero-worker install, byte-identical result file, and no routing decision taken.

### Stage 2 — routing

Scope: roster compiler, router pass, per-recipient folders, worker states, the supervision timer.

Acceptance:
- Replay: same roster and task in, same deliveries out, with no pool running.
- A declared set writes one sentinel per member and exactly one payload.
- An unavailable target holds and reports; nothing is written to another folder.
- A missing roster refuses rather than defaulting to the core.
- Concurrent claim and release leave exactly one winner, the loser seeing `OSError`.
- Removing the last worker returns the install to the Stage 1 state, and the same code runs in both directions.

### Migration

Both intakes coexist for a release or two. The rule that must hold: **a task is delivered exactly once.** The Task Bridge stamps which intake owns a task at admission, and the old path skips anything stamped for the new one. Removing the old path is its own change, after a release with no dual-path incidents.

## Out of scope for v1

Each of these is out of scope for v1, and none is an oversight.

- **Bindings that change themselves** are out of scope — sticky affinity, learned or decaying bindings, and the `affinity` file the current implementation carries.
- **Auto-scale** in either direction is out of scope; the router reports saturation and does not resize the pool.
- **Cloud or remote workers** are out of scope: `os.rename` is atomic on one filesystem and is not atomic across a replicated one.
- **A database-backed store** is not used; the filesystem is the store.
- **A scheduling loop inside a worker** is out of scope — its crons are not one.
- **Dedup, access tiers, envelope shape and result markers** are not covered here; they have their own owners.

## Open questions

1. Migrating in-flight work when the layout moves to per-recipient folders.
2. Delegation's depth and volume budgets, and the authorisation record behind them.
3. The parent contract for a set: completion, cancellation, downstream idempotency.
4. How a remote worker carries request, assignment and claim without a shared inode.
5. Identity persistence versus context restoration — resuming is not remembering.
6. How a steering message reaches the in-flight task it steers.
7. Per-worker schedule durability, and suppression on pause.
