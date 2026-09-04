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
"Lead", "follower" and "router" are retired as design terms.
The shipped artifacts that still carry them (`pool-lead.alive`,
`pool-lead-daemon.py`, the `com.sutando.pool-lead` launchd label, the health
probes keyed on them) are tolerated until steps 3 and 4 replace them; nothing
new is named after them.

## The starting point is zero workers

A fresh install runs the core and nothing else. That is not a degraded pool; it
is the default, and every mechanism below is inert in it. Nothing pool-shaped
runs until the command that creates worker 1, and removing the last worker
leaves the install exactly as it started. Single-worker mode is therefore not a
mode the pool has to detect; it is the absence of a pool.

## Who does what

| component | may do | must not do |
|---|---|---|
| **core** | create, destroy and resize the pool; choose a worker's model; execute every lifecycle command; write the pin table; claim every task addressed to no live worker; sweep worker beats, reclaim, revive, report | claim a task addressed to a live worker |
| **worker** | claim tasks **addressed to it**; execute them | claim anything else; create, destroy or reclaim anything |
| **launchd** | keep the processes it was given alive | decide how many there are |
| **app / bridges** | produce intent as owner tasks; render `state/pool-status.json` | call an API into the pool or touch its state |

No component does two of these. Saturation is something the core **reports**
in its status line ("queue waiting, all N workers busy"); that line is an ask
to the owner, never an act. Creating a worker is a spend decision, and it
belongs to the core on the owner's word.

## Routing policy: worker only when obvious, otherwise the core

A worker claims what is **addressed to it**. Addressing is the general idea;
v1 ships two ways of expressing it and leaves room for more, because a later
way to address a worker is a new input to the same rule, not a new rule. The
core claims whatever no live worker is addressed by.

Routing is therefore two pieces of data and one rule, evaluated by each watcher
for itself. The data, both of them forms of addressing: the envelope field **`requested_worker`**, which the server
writes at ingress when the sender addressed a worker (one envelope per
addressed worker, so a message addressed to three workers arrives as three
tasks with three ids); and the **pin table**. A sender's text is never a
routing instruction: the field is the server's decision, the bridge records it
verbatim, and nothing on the sutando side writes it.

The rule runs inside each watcher's own event handler
(`SUTANDO_TASK_EVENT_HANDLER`, read per process, so the core and every worker
carry their own), at the handler's probe, before any claim:

**Every emitter claims first.** An instance that decides it may emit acquires the
task-specific claim (a hard link in `state/task-event-handler-claims/`, keyed on the
task's CANONICAL ID — see below) and emits only if it wins; a loser suppresses. This holds for the
named target, the pinned target, a bound-set member and the core stand-in alike —
being addressed selects a *candidate*, it does not confer ownership.

**"Fresh" below means ELIGIBLE, not merely beating.** A target is eligible when
its beat is fresh *and* the core's sweep has not declared it wedged under §3 rule 6
(a pinned room's oldest unclaimed task older than `stand_in_after_s` while that
target holds no claimed task). A wedged target is treated exactly as a stale one:
the pin is unclaimable, so it falls through to rule 3 and the core stands in.
Without this, rules 1 and 2 suppress on liveness alone and a handler that keeps
beating without ever claiming holds its pinned tasks unclaimed indefinitely —
the precise state §3 rule 6 promises the core will take over.

Eligibility is **one value, computed once**: the core evaluates it in its sweep
(it is the only instance that can see a room's oldest unclaimed task) and publishes
it alongside the pool status; handlers read that verdict rather than each
recomputing it from beats they can only partially observe.

1. `requested_worker` names this instance: claim, then emit to this session.
   Names another instance whose beat is fresh: **suppress**.
   Names an instance whose beat is stale: the core claims and emits (stand-in),
   workers suppress.
2. No field, and the room addresses this worker — it is **pinned** to it, or
   this is a **dedicated** worker's own room: claim, then emit. Pinned to a bound set
   this worker belongs to: members race for the same claim; the winner emits and the
   losers suppress. Pinned to a FRESH instance that is not this one: **suppress**
   — the core included. A pin is addressing, so the core is a non-target here
   exactly as a worker is, and rule 3 must not be reached. Pinned to an instance
   whose beat is stale: the pin is unclaimable, so it falls through to rule 3 and
   the core stands in.
3. The task is addressed to no live instance: the core emits and workers suppress.

**Suppress means: take no claim, emit nothing, queue nothing, and leave the task
file untouched.** It is not "claim and discard", and the difference is the whole
correctness argument. The claim is keyed on the task's CANONICAL ID and is
therefore global across instances — `acquire_task_claim` hard-links
`state/task-event-handler-claims/<task-id>` and the first linker wins
(`src/watch-tasks-stream.sh:127-147`, which keys on the filename today and is
part of step 2's change). Suppression and claim-before-emit answer two
different races, and both are needed. Suppression removes the non-target/target
race: a non-target that claimed-and-discarded could win the claim and rename the
file before the target's session read the path it had been handed. Claim-before-emit
removes the target/target race: **addressing is not exclusive**, so two instances
can each correctly believe they are the addressee, and only a claim on a key
both compute identically decides between them. An earlier version of this section argued that
racing two discards is safe — true, and insufficient, because neither of the
races that matter is discard-against-discard.

**The claim key is the canonical task id, and that id comes from the shared
resolver.** Step 2 has the claimant rename `task-X.txt` to `task-X.claimed-W.txt`
(§3 rule 1), and the watcher dispatches every direct-child `*.txt` on Created **and
Renamed** with no lifecycle-name exclusion (`src/watch-tasks-stream.sh:639-702`). So
a key taken from the basename mutates under the very operation this design
prescribes: the rename re-enters as `task-X.claimed-W.txt`, whose key nobody holds,
and the task is emitted a second time or the rename cycle repeats.

The id is `task_archive.task_id_for(path, accept=...)`, not a suffix strip. Those
are not the same function: `.claimed-<x>` is a legal tail for a gateway id, so
stripping it re-aliases a real task onto another id, and only the persisted `id:`
header tells a gateway id from a pool rename — which is why that helper treats the
header as the positive authority and falls back to the filename only for a file
that declares nothing (`src/task_archive.py:22-93`). Step 2 delegates to it rather
than restating the rule, because a second implementation of one identity is the
same defect as a second enumerator of one question.

Excluding `.claimed-*` / `.assigned-*` from dispatch is the cheaper first gate and
stops the event being raised at all; step 2 owes both. It also owes three tests: a
real lifecycle rename, an id whose own tail looks like a state suffix (which must
NOT be stripped), and restart rediscovery — an initial sweep meeting a `.claimed-*`
file left by a previous run fails the same way with no rename in sight.

**Prerequisite for step 2, stated as such.** No suppress disposition exists today.
The probe's handled results are exit 0 (queue a fallback handler), exit 4 (queue a
required handler), exit 3 (emit directly) and an else branch that also emits
(`:385-402`) — every one of them either emits or queues, so a non-target watcher
currently cannot be inert. **And a second prerequisite:** the emit path takes no
claim today — probe exit 3 prints `TASK_FILE:` directly (`:398-400`) — so
claim-before-emit is a change to that path, not a rule expressible over it. Step 2
must add both the suppress disposition and the claim on emit before this rule can
be implemented; until then the rule is a contract, not a description.

Traced for the case the rule used to get wrong — a task with no
`requested_worker` in a room pinned to worker-2, with worker-2 fresh — and
scheduled adversarially, with **both non-targets running to completion before the
target's handler is even entered**:

| step | instance | disposition | shared claim | task file |
|---|---|---|---|---|
| 1 | core     | pin names a fresh instance, not me: suppress | untouched | untouched |
| 2 | worker-3 | pin names a fresh instance, not me: suppress | untouched | untouched |
| 3 | worker-2 | pin names me: claim (wins, uncontended), emit | held by worker-2 | consumed once |

Ordering is irrelevant here: run steps 1 and 2 in either order, any number of
times, and the state they observe and leave is identical.

**But that trace proves nothing about exactly-once**, because nothing contended.
The two schedules below are the ones that do, and both turn on the same fact:
*two instances can each correctly believe they are the addressee.*

**Trace A — the heartbeat crosses stale between two evaluations.** Freshness is
read per watcher, from a beat that ages in real time, so there is no instant at
which all instances agree on it:

| t | instance | reads | claim | emits |
|---|---|---|---|---|
| 89.9s | worker-2 | my own beat; `requested_worker` names me | **wins** | yes |
| 90.1s | core | worker-2's beat now stale -> I am the stand-in | **loses** | no, suppresses |

Without the claim both emit and the task is delivered twice. With it the core's
`link()` fails against worker-2's existing entry, so the stand-in stands down.
The reverse interleaving is equally fine and is the point: whichever runs first
wins, and the loser suppresses rather than deciding it was wrong to be a candidate.

**Trace B — an atomic repin between two evaluations.** Each watcher independently
re-reads mutable `bindings.json`, so an atomic replacement still hands two readers
two different valid versions:

| t | instance | reads `bindings.json` | claim | emits |
|---|---|---|---|---|
| t0 | worker-2 | pin -> worker-2 (old, valid) | **wins** | yes |
| t0+e | *(repin)* | `os.replace` swaps in pin -> worker-3 | — | — |
| t1 | worker-3 | pin -> worker-3 (new, valid) | **loses** | no, suppresses |

Neither read is torn and neither instance is wrong; atomic replacement rules out a
malformed read, not two valid versions selecting two targets. Only the claim
arbitrates, and it does so without either watcher having to observe the repin.

**The stale-target stand-in is Trace A**: the core reaches rule 1's stand-in branch
or rule 3 exactly when a worker's beat has aged out, which is the transition the
table above schedules against. It carries no exemption — the core takes the same
claim on the same key and suppresses on a loss.

In all three schedules the task file is consumed exactly once, by whichever
instance holds the claim, and every loser leaves the file untouched.

With worker-2's beat STALE the second column falls through for everyone, rule 3
selects the core, and the workers suppress. The same adversarial ordering holds —
both workers suppress without taking a claim, the core then takes the claim on
that key, wins it uncontested, and consumes exactly once. The stand-in is not
exempt from the claim; it is simply the only instance contending for it here.

The bound-set case is the one place the claim arbitrates target-against-target,
and it is the case it was designed for: every member is a target, so the winner
emits and the losers suppress rather than discard. Elsewhere the claim is still
load-bearing — Trace A is target-against-stand-in — but only ever between one
target and the core.

A task is eligible to exactly one instance except inside a bound set, where the
claim settles it. A later form of addressing (an app control, a room command, a
skill that binds a task to a worker at mint time) enters at the same point: it
either sets `requested_worker` or writes the pin table, and the rule above is
unchanged. There is no least-loaded fallback, no automatic binding of a
room to whichever worker took its first task, no lane busy-cap, no
saturated-pool overflow and no least-recently-picked tie-break. Rooms bind only
by an explicit pin. Each of those was a shipped rule in #3604's `_pick()`; each
is a later PR if the owner asks for it, and none is assumed here.

#3787's `pool_routing.py` does **not** implement this rule as it stands: at
`aab2e473` its `home-first` configuration ignores affinity and returns the home
seat for pinned and unpinned rooms alike (`pool_routing.py:167-176`), selecting
a worker only on the retired `target_worker` header. v1 has no dependency on
#3787; if it lands first it must become pin- and `requested_worker`-aware
before anything here uses it.

`requested_worker` supersedes the retired `target_worker` / `fan_out` headers
(one writer, the server, instead of a sender-controlled field) and #3859's
binding at ingress (`_bound_dest()` in the sparrow bridge writing
`.assigned-<worker>` into the filename): the bridge records a field, it does
not decide one. #3859 closed on this document.

## The binding table

`state/pool/bindings.json`, an object keyed by room id:
`{"<room>": {"instance": "<name>", "pinned": true}}`. One writer (the core) and
N readers (every worker, on every claim), so it carries the contract this repo
has twice been bitten for lacking:

- **Write:** temp file plus `os.replace` in the same directory, never `>` and
  never read-modify-write. A reader inside a truncate window saw zero bytes and
  read it as "no bindings" — #3156 is that shape on `core-status.json`, and
  `scripts/core-status.sh` exists so a caller cannot get it wrong.
- **Read:** every claim re-reads; there is no cached copy to invalidate.
- **Missing, unreadable or unparseable: fail toward the core.** The reader
  answers "not bound", the task falls to the core, and work continues. Failing
  closed to "not mine" would strand every task on a corrupt file. A binding
  exists to stop scatter, never to stop work.
- **Only `pinned: true` binds.** A bare entry without it is decayed handler
  state, not an owner's binding, and must not constrain routing.

`state/cores/channel-<room-id>.handler` is the automatic room-to-instance
affinity this design retires: it binds a room to whoever handled it first, which
the routing section excludes by name. Step 3's writer ignores it and deletes it;
two files answering "whose room is this" is worse than either alone.

## Commands: the owner's intent arrives as an ordinary task

Every pool command is an owner task, written by whatever surface the owner used
(the ag2space app's picker or a create-worker control, a room message, voice, or
the CLI skill), enveloped and verified like any task. One channel, one shape, one
code path to test.

**The discriminator is its own field, `pool_command:`, and NOT `source:`.** An
earlier draft of this section used `source:` to name the intent, which cannot
work: `source` already carries transport provenance and two live consumers
depend on it. `default_priority_for_source()` (`src/task_priority.py:38-55`)
branches on it to assign urgent/normal/low, and Telegram crash recovery claims
only the tasks that bridge wrote by testing `headers.get("source") != "telegram"`
(`src/telegram-bridge.py:~580`). So preserving `source: telegram` leaves the
pre-pin predicate unable to recognise a command, and overwriting it silently
disables that bridge's recovery and mis-prioritises the task. One field cannot
answer "which transport delivered this" and "is this a pool command" at once.

`pool_command` carries the command KIND (`resize`, `pin`, `unpin`,
`create-worker`, …). `source` keeps its transport meaning untouched, so priority
and crash recovery are unaffected.

The core executes commands, and a pinned room must not divert them: a command
envelope carries `requested_worker: core`, set by the surface that minted it, and
every worker's handler suppresses a task carrying `pool_command` **before** it
evaluates pin eligibility — suppress in the same sense as the routing rules
above, since `requested_worker: core` makes every worker a non-target and a
discard here would race the core's emit exactly as it would there. Both fields
live in the verified envelope, never in the body, so a room message whose text
reads "resize to 0" is an ordinary task.

**"In the envelope" means minted before the stamp, not appended after it.**
`stamp_text` MACs the whole body, so a field appended post-mint either
invalidates the stamp or displaces it, and a displacing edit reads `unsigned`
rather than `invalid` — tamper is not always loud. The server writes
`requested_worker` and `pool_command` into the body it then stamps, the way
`collaborator` already is (its append at `remote_gateway_bridge.py:2722` feeds
the same list the single stamp call at `:2811` hands to the stamper; CLAUDE.md's
"bypassing `serialize_task_last`'s key check" means the key allowlist, not the
stamp).

Being inside the stamp is not the same as being verified, which is why step 2
still owes the test: `apply_task_stamper` is fail-open by design, returning the
text unchanged when no stamper is injected and when one raises, so a task is
never lost but may be unstamped. "The field is in the verified envelope" is
therefore a property of a stamped task, not of every task. The step-2 suite
carries two negative tests: a forged body line, and a `requested_worker` added
after stamping, refused on the `unsigned` verdict rather than honoured.

**So fail-open signing needs a stated disposition, or a legitimate command
emitted during a signer outage has nowhere to go.** Refusing unsigned routing
fields without saying what happens to the task leaves exactly two outcomes, and
both are wrong: it reaches a worker in a pinned room — the bug this section
exists to prevent — or it is silently stranded. The matrix is therefore explicit,
and the invariant is that **an unverified command is never worker work**:

| stamp verdict | `pool_command` honoured? | `requested_worker` honoured? | disposition |
|---|---|---|---|
| **verified** | yes | yes | the core executes the command. |
| **unsigned** (no stamper, or the stamper raised) | **no** | **no** | refused as a command AND withheld from every worker. Quarantined with the reason, and the owner is told the command did not run and why. It is not retried silently — a command that changes pool topology must not execute on an unverified envelope. |
| **invalid** (stamp present, MAC mismatch) | **no** | **no** | same refusal, quarantined as tamper rather than outage, and reported loudly. |
| **unverifiable** (no local key, or a present-but-corrupt one) | **no** | **no** | same refusal and the same withholding, but classified as a LOCAL OUTAGE, not tamper: the key is at fault, not the file. Held rather than quarantined, and the owner is told the host cannot judge envelopes at all — every command is refused until the key is restored. |

Every non-verified row fails SAFE in the sense that matters here: the task never
becomes worker work and never mutates the pool. There are four because the shipped
verifier returns four — `verify_text` yields `verified` / `unsigned` / `invalid` /
`unverifiable` (`src/task_envelope.py:133-139`), and a matrix that assigns
dispositions to three of them leaves the fourth to whatever the caller does by
default. That module's own docstring says enforcement must fail closed on
`unsigned`/`unverifiable` and warns that `invalid` is not the only bad case. What it costs is availability
during a signer outage — commands stop working until signing recovers — and that
is the correct trade for an operation that resizes or re-pins the pool. The step-2
suite owes one test per row — four, not three — including the positive control
that a verified command still executes, or the matrix is prose that never ran.

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

1. **Claim:** exclusivity is the watcher's hard-link claim, keyed on the
   CANONICAL task id — `state/task-event-handler-claims/<task-id>`, resolved by
   `task_archive.task_id_for(path, accept=...)` (first link wins, a dead owner's
   claim retired by pid); the claimant then renames `tasks/task-X.txt` to
   `tasks/task-X.claimed-<name>.txt` as the durable record the sweep reads.
   Keying on the raw basename would hand the renamed file a key nobody holds.
   There is no assignment step; eligibility is `requested_worker` and the pin
   table.
2. **Done-flags:** `state/cores/<name>/done/task-X.flag` is written before any
   external side effect; the at-most-once floor is unchanged.
3. **Heartbeat / lease:** per-process `.alive`, 30 s beat, 90 s stale.
4. **Reclaim:** the core, in its sweep, reclaims a task claimed by a worker
   whose beat is stale and one claimed with no result evidence, behind the
   done-flag. Workers reclaim nothing.
5. **Stand-in:** while a pinned worker is ineligible — beat stale, or wedged
   under rule 6 — the core claims that room's tasks itself, and stops when the
   worker is eligible again. A fresh beat alone does not end a stand-in; rule 6
   is what says whether beating counts. The pin is not changed; nothing is
   loaned or re-bound.
6. **Busy is not hung.** A worker with a fresh beat and a claimed, unfinished
   task is busy, and the core leaves its rooms alone. The core stands in for a
   fresh-beat worker only when a pinned room's oldest unclaimed task is older
   than `stand_in_after_s` (default 300) **and** the worker holds no claimed
   task, plus one sweep of grace after its last finish. This is the claim-only
   form of #3604's claimed-load, busy-deferral cap and post-busy grace
   (`pool_lead.py:514-547`), so a long task never has the core answering the
   same room concurrently.

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
mtime, then claims the top. That contract holds only for task-last writers:
the gateway's `_TASK_FIELDS` places `task` before `priority`
(`remote_gateway_bridge.py:1592-1604`), so the safe parser reads gateway
priorities as absent and a gateway `urgent` sorts as `normal`. Converging the
gateway writer on task-last is a prerequisite of step 2, its own PR. Because candidate sets are fixed by the pin table,
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
| beat fresh, no claimed task, a pinned task unclaimed past `stand_in_after_s` | hung session | the core stands in for the room; the kick script un-wedges an idle prompt |
| beat fresh, credentials valid, every turn returns an out-of-credits error | **quota spent** | **quiesce** — the worker stops claiming until its window resets and the core stands in. Kicking is worse than nothing: the seat still claims and then fails, so the task leaves the unclaimed state and the stand-in never fires. Measured live: four seats beating normally at zero throughput |
| beat fresh, a claimed task unfinished | busy | leave it; the room waits for its worker |
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
2. worker side: the per-instance event handler (eligibility from
   `requested_worker` and the pin table, priority order, done-flags) and the
   worker heartbeat. Prerequisites, each its own PR because each edits an
   existing file: the gateway writer converges on task-last, **with its
   `parse_task_headers_trusted` readers migrated in the same PR** — that parser
   is only valid for a task-mid writer, so converging the writer without the
   readers would leave a last-wins parser reading `access_tier` off a task-last
   file, resting the trust boundary on an unstated invariant (the body flatten
   still stands behind it, so this is a sequencing rule, not a live hole). The
   sort defect alone can be fixed first and separately by moving `priority`
   ahead of `task` in `_TASK_FIELDS`, exactly as #3872 did for
   `requested_worker`; that decouples the user-visible half from convergence.
   Its own PR against an existing file, and the only one left: the watcher
   sentinel becomes per instance (`state/watch-tasks-stream-<name>.pid` and its
   four readers, since N watchers on one host overwrite the single file today)
   and the fallback-receipt directory with it. The sparrow bridge carrying
   `requested_worker` is **already done** by #3872 rather than pending —
   exercised through `_write_task`, the field is serialized above `task:` and
   the safe parser reads it back, so nothing further is owed there.
   **Not a prerequisite PR, a
   requirement on step 2's own code:** the eligibility reader matches both
   `channel_id` and `chat_id`. No such reader exists on `main` — checked, it
   has never been there — so there is nothing to fix ahead of time; the failure
   is a Telegram-addressed task silently reading as unbound the first time
   someone pins a chat, and step 2's suite carries that case.
3. core side: the pin table writer, the sweep (reclaim, stand-in, revive,
   status line, timing record), with claim and liveness tests;
4. installer and plists, including per-worker model;
5. later, each alone: the app's pin and create-worker controls, dedicated
   workers, pool status push.

Each merges before the next opens. #3604's children re-cut against the new
base after step 3.

**The pool running today is migrated, not assumed away.** Between step 2 and
step 3 the worker handler exists and the core sweep does not, so in that window
the current lead keeps reclaiming and the new handler claims only what the
binding table sends it; a host enters step 2 with its lead either running or
deliberately stopped, never ambiguous. Step 3 takes reclaim over in one cut. Two
on-disk carry-overs are step 3's to resolve rather than inherit: `.claimed-*`
and `.assigned-*` files minted by the lead are re-examined by the new sweep
behind the done-flag, and a seat from an older generation — a different binary
under the same instance name — is recycled rather than trusted, since `.alive`
is keyed by name and cannot tell generations apart.

## Out of scope for v1

A router process: it earns a process only when a policy needs to see the whole
queue at once (burst consolidation for `[deduped:]`, fairness across sets,
auto-scale), and every one of those is out of v1. Fan-out is in, but only in
the server-side form above (one envelope per addressed worker); a sutando-side
fan-out of one task to N workers is out. Also out:
auto-scale in either direction; router-minted task ids (the admission
/ census gap stays its own track, and bridges keep minting ids as they do
today); idle-timeout rebalancing and the channel-affinity freshness gate (rooms
bind only by pin, so there is nothing to rebalance); lane routing. Install
troubleshooting (TCC under `~/Documents`, config-dir capture, error-log marks)
and `--only-core` / uninstall semantics belong to the installer PR's own doc.
