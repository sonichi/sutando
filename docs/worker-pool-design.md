# Worker pool — design (v1)

**Status:** design, owner-decided 2026-09-03 (PR-triage room). This is step 1
of staging #3604 into PRs against `main`; #3604 stays open as the reference
implementation and is not merged as one piece. It supersedes the
"lead = the runtime daemon" and "lead-managed sizing" placements in #3604's
`docs/lead-follower-pool.md` and Decision 4 of
[`core-pool-standing-sessions.md`](core-pool-standing-sessions.md), and Decision 5
of that record (the unclaimed-work backstop belongs to the lead; followers stay
purely event-driven) — superseded because it sites the backstop on a lead this
design no longer has, not because its reasoning was wrong; see **The reconciliation
ticker**, which answers its O(N) objection rather than dropping it.

**It also supersedes Decisions 2 and 3 of that record, explicitly.** Decision 2 makes the
binding unit a CONTEXT GROUP; Decision 3 promises *at most one outstanding assignment per
context group*, enforced by the lead refusing to create the second one. This design is
room-keyed and has no lead, so neither survives: **the refusing party does not exist.** An
earlier revision of this document claimed that cutting the `exclusions` rule dissolved the
conflict. It did not, and that claim is retracted here — `exclusions` was one way grouped
rooms ended up on non-coordinating workers, never the reason the binding unit is a room.
What replaces them: **the binding unit is the ROOM, and concurrency is bounded per TASK by
the claim, not per group by an assigner.** Two rooms of one former context group may run
turns at the same time, and so may two members bound to one room. Group identity, group
admission and group release are named as out of scope for v1 in the routing section and
remain so; a v2 that wants Decision 3's guarantee back must build them, because nothing in
v1 can express it. Every other decision in that record stands.

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
| **core** | create, destroy and resize the pool; choose a worker's model; execute every lifecycle command; write the pin table; claim every task addressed to NO WORKER AT ALL (an UNBOUND room) -- never one addressed to a bound-but-stale worker; sweep worker beats, reclaim, revive, report | claim a task addressed to a live worker |
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
core claims whatever is addressed to NOBODY -- an unbound room. A bound room whose worker is stale is NOT that case.

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
named target, the pinned target, the room's bound instance, and the core on an unbound
room alike — being addressed selects a *candidate*, it does not confer ownership.

**"Fresh" below means ELIGIBLE, not merely beating.** A target is eligible when
its beat is fresh *and* the core's sweep has not declared it wedged under §3 rule 6
(the oldest unclaimed task addressed to it — by any route, not the pin alone —
older than `stand_in_after_s` while it holds no claimed task). A wedged target is treated exactly as a stale one:
the pin is unclaimable, so the task stays PENDING — it does not fall through to
rule 3, and nothing else claims it.
Without this, rules 1 and 2 suppress on liveness alone and a handler that keeps
beating without ever claiming holds its pinned tasks unclaimed indefinitely, with
nothing recording that it is doing so. Rule 6 does not rescue that state by taking
the work — v1 has no stand-in — it NAMES it: the wedged verdict makes the pin
unclaimable, so the task is visibly pending instead of sitting claimed-by-nobody and
unreported. **The verdict does not clear itself** — it freezes both of its own inputs, so
no automatic exit exists. The exit is the commanded reset — see "Clearing a wedged verdict"
under §3 rule 6.

Eligibility is **one value, computed once**: the core evaluates it in its sweep
(it is the only instance that can see a room's oldest unclaimed task) and publishes
it alongside the pool status. **WORKER handlers read that verdict to gate
themselves; the core's handler never does** — it routes on beats, and the eligibility
decision belongs to the sweep that wrote the verdict. Who reads what, and
why one reader per decision is load-bearing rather than tidy, is set out under the
record's contract below; this rule is step 3's, and step 2 routes on beats alone.

**That publisher is a step-3 component, and the handler ships in step 2** — so the
wedge path has a window with a consumer and no producer, which is a latent no-op at
best and a silent misroute at worst. It is resolved by a stated default rather than
by ordering luck: **when the record is absent, unreadable, or stale, every
fresh-beat target is eligible** — exactly the behaviour before this rule existed, so
step 2 is a no-regression change and the wedge path simply does not fire until step
3 lands the sweep. Defaulting the other way would divert healthy pinned work to the
core for the whole window, breaking the pin contract to fix a case that cannot yet
be detected. Step 2 ships **no reader at all** — the default is its whole rule, so
there is nothing to test absent-or-corrupt against yet; the reader and those cases
arrive together in step 3, listed in its scope below. Step 3's suite pins absent,
corrupt, stale, future-clock and the old/new transition.

The record's contract, because a routing-critical shared file without one drifts:
**one writer** (the core sweep; a worker that writes it is a defect, not a
fallback), written **atomically** by temp-file plus `os.replace`, the temp file in
the DESTINATION DIRECTORY — `os.replace` is only atomic within one filesystem, so a
temp in `/tmp` degrades to a copy and a reader can see a partial file, which is the
one thing this rule exists to prevent. This is the same one-writer/atomic/fail-toward-absence shape
`core-status.json` already uses, and for the same reason: a truncated read of a
status file was taken as a verdict once already.

Shape, pinned exactly — "a per-instance verdict" is not a contract, and the reader
and the writer are separate components even though step 3 ships both. It lives
under the `eligibility` key of `state/pool-status.json`:

```json
{"eligibility": {
   "version": 1,
   "computed_at": 1788546000,
   "stale_after_s": 180,
   "instances": {"worker-1": "eligible", "worker-2": "wedged"}}}
```

`computed_at` is **Unix seconds**, integer. `stale_after_s` travels **inside the
record** rather than being derived from the sweep interval, because a reader must
not have to know the publisher's cadence to judge freshness: a bound stated as
"two sweep intervals" is unevaluable by anything that does not already share the
publisher's configuration, and that includes the core, an operator reading the
file by hand, and any later consumer. The publisher says it outright.

An earlier draft justified this by a **step-2 reader** predating the sweep. That
reason was wrong and contradicted this document in three other places
(`:89-100`, `:162-177`, `:929-934`): **step 2 ships no reader at all**, and the
reader arrives with the step-3 publisher. The conclusion survives its retracted
premise — self-describing staleness is right because a consumer should not need
the producer's config, not because of a staging gap that does not exist. `instances` maps an instance name to the enum
`eligible` | `wedged`, and nothing else.

Validation is a matrix, and **every failing cell means ABSENT for that instance**,
which by the default above means eligible-if-its-beat-is-fresh:

| condition | verdict for that instance |
|---|---|
| file missing, unreadable, or not valid JSON | absent |
| `version` absent, not an integer, or not one this reader implements | absent |
| `computed_at` absent, not an integer, or more than 60 s in the FUTURE | absent — a clock ahead must not confer permanent freshness |
| `now > computed_at + stale_after_s` | absent (stale) — never its last value, or a dead publisher keeps a worker diverted forever |
| `stale_after_s` absent, not an integer, or outside **60..600** | absent — see the bound below |
| the instance is not a key of `instances` | absent — an unlisted instance is unjudged, not judged clean |
| its value is not exactly `eligible` or `wedged` | absent |
| any other key present anywhere | ignored, so the writer may add fields without stranding an old reader |

**v1 fixes `stale_after_s` at 180, and a reader REFUSES anything outside 60..600.**
An unbounded positive integer defeats the expiry guarantee two rows above: a record
carrying `stale_after_s: 9223372036854775807` passes every cell of this matrix and
then preserves a `wedged` verdict for the life of the host after the publisher dies —
the dead-publisher case the row was written against, arriving through the field meant
to bound it. The reader enforces the range because it is the party that survives a
buggy or hostile writer; the writer emitting 180 is a convention, the reader's refusal
is the guarantee. 60 is below no plausible sweep interval and 600 is ten minutes of
stale routing, which is the most this design is willing to owe.

Absent is the ONLY failure mode, deliberately: it collapses every partial-read
disagreement onto the pre-rule behaviour, so two conforming readers cannot choose
opposite eligibility for one bad record. `version` is what makes a shape change
safe — a reader that does not implement the version reads absent rather than
guessing at fields it does not know, and the step-3 suite exercises a reader
meeting both an old and a new record across the atomic swap, since that is the one
moment two versions coexist on disk.

**The whole eligibility record is a STEP-3 mechanism, and step 2 never reads it.**
The obvious reading — both sides read it, and a disagreement resolves at the claim —
is wrong in one direction: a claim arbitrates N>=2 candidates and cannot manufacture
a missing one. Worker-reads-old(`eligible`) against core-reads-new(`wedged`) gives
two claimants and the claim decides; but core-reads-old(`eligible`, so it suppresses
as a non-target) against worker-reads-new(`wedged`, so it suppresses too) gives
**zero**, and suppression leaves the task file untouched. The task strands, which is
the exact outcome the eligibility rule was added to prevent.

Two readers of one racing file is the defect, so the staging removes the second
reader rather than arbitrating between them:

**Step 2 routes on BEATS ALONE.** No instance consults the record, because there is
no publisher yet — the default stated above (absent record means every fresh-beat
target is eligible) is therefore the *whole* rule in that window, not a fallback
inside it.

**That does NOT make the zero-candidate schedule go away, and an earlier revision of
this section claimed it did.** The claim was that beats are observed directly rather
than raced through a file, so two instances cannot disagree. False, and the trace
below already says why: each watcher samples an AGING beat independently. The core
reads worker-2 fresh and suppresses; the beat crosses stale; worker-2 reads itself
stale and suppresses. Nobody claims. The pin swap has the same shape in reverse — a
worker reads the old pin and suppresses, the pin swaps, the new target reads the new
pin and suppresses, and the core sees a fresh target under either version. **The hole
belongs to suppress-based routing over independently-sampled state, not to the
record**, so removing the record does not remove it.

**So suppression is never terminal, and the re-evaluation is owned by the WATCHER,
on its own 30s reconciliation.** An earlier revision put it on the heartbeat, which has
no execution path to it: `src/core_heartbeat.py` is a detached liveness sidecar started
as its own process (`startup.sh:690-697`) and contains ZERO references to
`dispatch_task`, `acquire_task_claim` or `TASK_FILE` — routing and claiming live in
`src/watch-tasks-stream.sh:127-147,370-404`. "On its own beat each instance re-runs the
routing rule" therefore named a process that cannot run it, and choosing the heartbeat
because it was already periodic put the work where the timer was instead of where the
capability is.

The watcher already owns every piece: it lists `tasks/*.txt` at startup, it holds the
hard-link claim, and `dispatch_task` IS the production emit path. The reconciliation
calls THAT — it does not get a second claim recipe of its own, which is the thing most
likely to drift from the first. On its own
tick the watcher re-lists the pending task directory and runs each file through
`dispatch_task` exactly as a Created event would; the ordinary claim arbitrates whoever
wakes, and a task this instance is not a candidate for suppresses as always.

**No age gate, and that is a decision rather than an omission.** An earlier draft
admitted only files older than one beat, which buys nothing and costs three things: a
suppress/suppress pair can then need TWO ticks rather than one, a 29-second-old urgent
task waits while an older low-priority one is admitted ahead of it, and a file with a
future mtime is either never eligible or immediately eligible depending on a comparison
nobody specified. Re-listing everything pending removes all three questions — a task
already claimed is skipped by the claim, not by an age test, so the gate was never
load-bearing. Order is the queue's existing priority order, not directory order. One
tick claims every file this instance is the candidate for **up to the ticker's own
admission bound** (defined under **The reconciliation ticker**), not "the same rule as
the event path" — the event path has no admission bound to inherit. Reconciliation is
not a second scheduler and must not be able to outrun what the instance can run.

**That sentence named a bound that does not exist, and the ticker cannot reuse what is not
there.** Production bounds EXECUTION, not ADMISSION: `queue_handler_task` takes the dispatch
lock, calls `acquire_task_claim` and writes the pending marker, and only then calls
`drain_dispatch_queue` — so the two-runner cap gates how many run, never how many are claimed.
Measured control, five tasks against an unchanged tree: **5 claims, 2 running, 3 pending.** An
event path that claims everything and runs two is correct for events, because events arrive at
the rate work arrives. A ticker re-listing the whole pending directory is not rate-limited by
anything, so "the same bound" would be no bound at all.

So the ticker needs an ADMISSION bound of its own. **It is a bound on TOTAL OUTSTANDING, not a
per-pass allowance** — a per-pass allowance adds its quota again every 30 s whether or not
anything finished, which is the same unbounded growth wearing a limit:

```
handler?    = DISPATCH_DIR is non-empty (src/watch-tasks-stream.sh:91 initialises it to "").
              THIS is how "no handler" is represented — NOT runners == 0. TASK_HANDLER_WORKERS
              is unconditionally 2 at :89, so a zero there is unreachable and cannot carry the
              inertness case. The ticker requires a pool, a pool requires a handler, so an
              empty DISPATCH_DIR means the ticker is not armed in the first place.
runners     = TASK_HANDLER_WORKERS (:89) while a handler exists.
outstanding = |DISPATCH_DIR/running| + |DISPATCH_DIR/pending| + |DISPATCH_DIR/direct|
throttle    = the RECONCILE caller admits only while outstanding < 2 * runners
```

**It is a throttle on reconciliation, not a global cap, and `outstanding <= cap` is NOT an
invariant.** Created/Renamed events stay unbounded so current behaviour is preserved exactly; only
the startup/ticker sweep is bounded. So the promise is the narrow one — *a bounded reconcile call
adds nothing once observed outstanding is at the threshold* — and an ordinary event may legitimately
carry total outstanding above it. A design claiming a ceiling would be promising something the event
path never agreed to. (Framing owed to keweichen; my earlier "admit while outstanding < 2*runners"
implied the ceiling.)

**The admission is one primitive in three phases, and the phase boundaries are what make it
implementable.** The invariant it serves: *while pool dispatch is armed, no queue marker or
`TASK_FILE` publication becomes visible except through one production-owned admission/transition
primitive* — with the empty-`DISPATCH_DIR` path as the explicit unpooled exception.

1. **Classify OUTSIDE the lock** — probe, route, and choose the caller mode
   (`bounded-reconcile` / `unbounded-event`). The probe is an external subprocess; holding a
   `mkdir` spinlock with no timeout across a process spawn would serialise every dispatch behind
   the slowest probe, which is worse than the race it would close.
2. **Commit UNDER one lock** — the shared owner checks the bound where it applies, acquires the
   canonical claim, and creates or transitions exactly one `pending` or `direct` receipt. It
   returns a typed outcome by out-parameter, since a bash function's exit status cannot carry
   `queued` / `direct` / `lost` / `suppressed` / `refused-over-bound` distinctly — and the
   **`fallback`-mode publish branch** must
   tell `refused-over-bound` apart from the others rather than folding them.
3. **Publish AFTER unlock** — queue or emit only from that admitted token. Stdout stays exclusively
   `TASK_FILE:`.

`queue_handler_task` therefore becomes a **downstream executor, not the admission owner** — which
is the correction that matters, because putting the primitive inside it left the four direct exits
bypassing admission entirely.

**A direct receipt is created by TRANSITION or by ADMISSION, and which one is a property of the
exit, not of the design.** These are two contracts and an earlier draft of this section stated only
the first, as though it held for every direct dispatch. It does not.

The discriminator is whether a receipt for that task already exists when the exit runs:

| exit | prior receipt | what it must do |
|---|---|---|
| handler fallback, disposition-1 | **yes** — the task was admitted when the handler took it | **transition** the existing receipt to `direct`; a second admission here is the double-count |
| a direct dispatch with no handler behind it (initial probe-direct, operational direct) | **no** | perform a **fresh admission** through the shared primitive and create the receipt |

So every direct exit must declare which case it is, and an exit that cannot say is a defect in the
exit rather than a gap in this contract. The rule that generalises both: **a task is admitted
exactly once, and `direct` is a state that admission can be entered into or moved into — never a
state that skips it.**

**Completion and release.** Admission without a defined release is a leak: the bound counts
receipts, so a receipt nobody removes lowers the ceiling permanently and the pool starves without
any component reporting a fault.

| | contract |
|---|---|
| owner | the process holding the receipt — the core or worker whose exit created or transitioned it. Never a third party, never the ticker on a live holder |
| trigger | the task reaches a terminal state: a result is written and archived, **or** the claim is released after a failure. Both paths release; only the terminal state differs |
| signal | the holder unlinks its own file under `direct/`, in the same step that writes the result |
| crash | the holder dies holding it. The **reconciliation ticker** is its only reclaimer, and it decides on the receipt's own PHASE — never on age alone. See the table below |
| failure | unlink is best-effort and idempotent. A double release is not an error, and a failed unlink must not fail the task — the ticker is the backstop |

**An mtime is not a phase, and reclaiming on age alone duplicates work.** A receipt that is
merely old cannot say whether the holder died *before* publishing the task or *after* — and those
need opposite handling. Unlinking and re-admitting in both cases means a watcher restart after
`TASK_FILE:` publication, or any direct task outliving `stale_after_s`, re-runs an owner task that
is already in flight. So the receipt carries its own phase and lease:

```json
{"task_id": "<id>", "holder": "<instance>", "phase": "admitted|emitted",
 "lease_until": "<iso>"}
```

The **holder is the sole writer**, transitions are one-way (`admitted` -> `emitted` -> unlinked),
`admitted` is written **before** the task file is published and `emitted` **after** the publish
returns, and the holder renews `lease_until` on the same beat as its `.alive`.

| phase | lease | the task's own CLAIM state | the ticker |
|---|---|---|---|
| any | live | — | not orphaned. Leave it; a live holder owns its own receipt |
| any | expired | task absent, or present and **unclaimed** | nothing is executing it, so re-admission cannot duplicate. Unlink and re-admit |
| any | expired | **claimed, unfinished** | a worker holds it. **Never re-admit.** The receipt stands until the task reaches a terminal state |
| any | expired | **terminal** (result written and archived) | the work is done. Release the receipt; its slot returns |

**The discriminator is the task's claim state, NOT the task file's existence.** An earlier revision of
this table keyed the ambiguous row on *task present* and read that as proof the publish had landed.
It is not: the task file is written **before** the notification, so it is present on both sides of the
emit and a crash in between takes the "already emitted" branch — the receipt then consumes a slot
forever for a task nobody ever received. Measured on the production `dispatch_task` by a reviewer:
`before emit: task_present=True phase=admitted emitted=0` / `after emit: task_present=True phase=admitted
emitted=1`, with the task and receipt bytes identical in both.

Claim state does not have that defect, because it answers the question the bound actually cares about.
Duplication is only possible when something is **executing** the task; an unclaimed task can be
re-admitted safely no matter which side of the publish the holder died on. So the phase field stops
being a decision input and becomes provenance — useful for an operator reading the record, never the
thing the ticker branches on.

**The admitted count is derived by listing `direct/`, never held in a counter**, so removing the
file *is* the decrement and there is no second number to drift out of step. This is the same defect
class the outcome contract above records: a queued winner that both wrote a marker and incremented a
counter admitted two against a bound of four. One artifact per admission, counted by listing it.

Handler fallback (`:255`, `:506`, `:542` — the disposition-1 branch) already emits a task that *holds a claim*: it
writes a `FALLBACKS_DIR` marker and calls `emit_fallback_task_file` / `emit_task_file`. So the task
was counted when it was first admitted, and moving it to `direct` is a state change on an existing
receipt rather than a new one. That is what closes the double-admission hole without a second
counter — the earlier drafts of this section kept looking for a way to *admit* a direct dispatch,
and the answer is that it was already admitted.

**The `fallback`-mode publish branch must separate refusal from failure, and today it cannot.**
That branch is the `queue_handler_task "$task_path" "fallback"` call and the `|| printf` that
follows it — `watch-tasks-stream.sh:390` at the time of writing, but the mode argument is the
identity and the line number is only a locator. It reads
`queue_handler_task ... || printf 'TASK_FILE: %s\n' ... || exit 0`, and `queue_handler_task`
returns non-zero for a lost claim *and* for an operational failure — so a generic `|| printf`
publishes in both cases and cannot tell `refused-over-bound` from a genuine error. The typed
outcome exists precisely so this branch stops guessing.

So the direct lifecycle completes the rule with five obligations, and they are the same five the
earlier revision listed as unowned: a durable receipt written **before** the emit, a named ownership
handoff, an idempotent completion acknowledgement, exactly one release writer, and restart handling for
THREE crash windows, and they are distinct states rather than one described three ways:

1. **`receipt-before-emit`** — the receipt is durable, the emit has not happened.
2. **`emit-before-ack`** — the emit happened, the completion acknowledgement is not yet durable.
3. **`ack-before-release`** — the ack IS durable and the single release writer has not finished.
   On restart, reconcile the existing receipt and complete the release **idempotently**: do not
   re-emit, and do not create a new admission. Keeping this separate from `emit-before-ack` is the
   point — collapsing them loses the distinction between "nobody has been told" and "everybody has
   been told and the bookkeeping is half-done", which need opposite recoveries.

(This section is keweichen's design. Two shapes of mine were falsified before it — one that put the
primitive inside `queue_handler_task`, and one that took the lock at `dispatch_task` entry and would
have held a no-timeout spinlock across a subprocess probe.)

**The named ownership handoff has to be a value the EXECUTOR reads, because today the claim's mere
existence means "not yours" and the direct path deadlocks on that.** Measured against the shipped
Codex consumer rather than reasoned about:

- `next_pending_task` skips any candidate with a claim file — `[ -f "$TASK_HANDLER_CLAIMS_DIR/$candidate" ] && continue`
  (`src/agent/codex/cli/task-notifier.sh:188`) — unconditionally, and BEFORE it consults the
  fallback marker, so no fallback event overrides it.
- `TASK_FILE:` is only a wake: the loop re-scans the whole queue through that same function
  (`:376-386`). There is no other trigger — `wait_for_core_idle` (`:166-179`) polls for IDLENESS,
  never for new work.
- The watcher persists and emits BEFORE releasing, deliberately (`src/watch-tasks-stream.sh:524`):
  *"duplicate delivery is acceptable here, while release-before-emit could permanently strand work."*

Compose those three and the direct exit emits a wake its consumer is required to refuse: the claim
is still present at wake time, the candidate is skipped, and the later release fires no second
event. The task stays durable and unsubmitted — not lost, but never delivered, which is worse than
lost because every surface reports it as pending.

**The transition is a SECOND record written by the EXECUTOR, not a field in the watcher's claim.**
An earlier revision of this section put the accepting executor's name on line 4 of the claim and
called that the handoff. It is not: **the claim is written by the watcher BEFORE the emit, so every
byte of it is identical whether or not anyone ever received the wake.** A name written by the sender
is an ADDRESS; acceptance is an OBSERVATION, and only the receiver can make it. The two crash
windows below are indistinguishable in the claim, however line 4 is spelled.

The shipped lifecycle makes that concrete, and it refuses two things the earlier revision asserted:

- `claim_is_live()` keys on **line 1, the WATCHER's pid** (`src/watch-tasks-stream.sh:101-109`), so
  a claim goes "dead" when the watcher exits even if an executor is mid-task on it, and
  `retire_stale_claim()` (`:116-124`) then removes it.
- `release_task_claim()` requires `owner_id == WATCHER_ID` (`:150-160`). An accepting executor
  **structurally cannot release**, so "release is the accepting executor's" was not a rule the code
  could obey.

**The offered -> accepted transition, with its writer, key and authority named.** A second directory,
`state/task-event-handler-accepts/<canonical task id>`, published by hard link exactly as the claim
is — same atomicity, same never-clobber semantics:

```
<accepting executor's pid>
<accepting executor's instance id>
<watcher id it accepted from>
<accepted_at>
```

| | contract |
|---|---|
| writer | the **accepting executor**, exactly once, after it has the task in hand and before it submits. One writer per file; a hard link that loses means another executor accepted first and this one suppresses |
| what the claim now means | OFFERED, addressed by line 4. Not taken. It is the sender's record |
| what the accept means | TAKEN. It is the receiver's record, and it is the only evidence that the wake was received |
| **the liveness key CHANGES HANDS** | unaccepted, liveness is the claim's watcher pid; accepted, it is the accept record's executor pid. `claim_is_live()` must consult the accept file before answering, or it keeps reporting a live task dead |
| release authority | the claim's `WATCHER_ID` **or** the instance named in the accept record. `release_task_claim`'s single-owner test has to widen to exactly that pair — no wider |
| this is a CODE CHANGE | not a restatement. `claim_is_live`, `retire_stale_claim` and `release_task_claim` are all named above because all three must change in the implementing PR. Saying so is the difference between a contract and a wish |

**Now the two windows are different states rather than different stories.**

| observed | means | recovery |
|---|---|---|
| claim live (watcher pid), no accept | in flight, nobody has taken it yet | leave it |
| claim dead (watcher pid), **no accept** | watcher died at or before publish; nobody received the wake | retire the claim, re-admit normally |
| claim dead (watcher pid), **accept present, executor live** | watcher died after publish; the executor owns the work | **do not retire** — this is the case the shipped sweep gets wrong today |
| accept present, executor dead | the executor died mid-task | reclaim both records behind the done flag |

The earlier revision could not express row 3 at all, which is why it had to ask the sweep to
"observe acceptance" that nothing wrote.

**Claude's wiring needs no change and that is a finding, not an omission.** Its consumer is this
document's own `Monitor`-driven reader, which acts on the emitted `TASK_FILE:` line directly and
consults no claims directory — so the deadlock is specific to the Codex consumer's skip rule. Two
executors reading the same emission under different rules is what let this ship: the design was
checked against the one that has no rule to violate.

**Which makes `next_pending_task` the FOURTH function the implementing PR must change, and naming
it here is the point.** The three above are named as obligations; this one was cited only as
*evidence* of the deadlock (`:408`), and a cause that appears solely in the diagnosis does not get
fixed. Publishing the accept record while that skip rule is untouched leaves the deadlock exactly
where it was: the claim is still present at wake time, the candidate is still skipped
unconditionally and BEFORE any other file is consulted, and the accept record nothing reads changes
nothing.

| | contract |
|---|---|
| today | `[ -f "$TASK_HANDLER_CLAIMS_DIR/$candidate" ] && continue` — presence of a claim means "not yours", with no way to express "offered TO you" |
| required | consult the accept record before skipping: a claim whose line 4 addresses THIS instance and that carries no accept yet is a candidate to ACCEPT, not one to skip. A claim addressed elsewhere, or already accepted by another instance, is still skipped |
| the key must match | `$candidate` is a raw basename today while the claim key is the canonical task id (`task_archive.task_id_for`). Both sides must compute the same key or the lookup misses in the safe-looking direction — a miss reads as "no claim", which un-skips a genuinely handler-owned task. **The key change and the skip change land together or the adapter gets worse, not better** |

That last row is why this is a migration rather than an edit. A reviewer's control on the shipped
function showed the two adjacent cases already diverge — a raw-filename claim suppresses the
candidate while a canonical-id claim does not — so shipping the canonical key alone would silently
stop suppressing handler-owned work, and shipping the skip change alone would look up a key that is
not there.

**Every admission leaves a receipt, and the ticker keeps NO counter of its own.** An earlier
revision had the ticker count "claims made this pass" and add that to the directory count. That
was wrong three ways at once, and the first is the one this section had already condemned in
its own words: a per-pass counter resets, so a receipt-less admission vanishes from the next
recount and every tick adds another `2 * runners` without any completion — *the per-pass
allowance wearing a limit*. Second, a queued winner both wrote a `pending/` marker AND
incremented the counter, so a bound of four admitted two. Third, and fatal to the idea: the
ticker cannot tell a lost claim from a won one, so "do not count a loss" was not implementable.
`queue_handler_task` returns 0 for BOTH — it releases the lock and returns 0 when
`acquire_task_claim` fails (`:360-363`), and returns 0 after writing the marker when it wins.

**What this section replaced, kept short because the falsifications are the useful part.** Earlier
revisions of this design said three things that are now false, and each was corrected by a reviewer
rather than by me:

- **a four-outcome `dispatch_task`** (`queued` / `direct` / `lost` / `suppressed`). The contract is
  five: `refused-over-bound` is a distinct outcome from operational failure, and the `fallback`-mode
  publish branch must tell
  them apart instead of falling through to a generic `|| printf`.
- **an ownerless `direct/` receipt** that step 2 would have to invent an owner for. For the **handler
  fallback** exit this is not a new admission — disposition-1 transitions an already-claimed receipt
  (`:255`, `:506`, `:542`), so it was counted at its first admission. That is the half this summary
  used to state as though it were universal. It is not: an **initial probe-direct or operational
  direct** exit has no prior receipt and therefore performs a fresh admission through the shared
  primitive, per the contract table above. Which of the two applies is a property of the exit.
- **`queue_handler_task` as the admission owner**, with the count-and-claim primitive placed inside
  it. That left all four direct exits bypassing admission, and it is why `queue_handler_task` is now
  a **downstream executor** instead.

One measured correction is worth keeping in full because it kills an obvious-looking fix: the claim
record is **not** a durable-receipt candidate. `claim_is_live` is `kill -0` on the owner pid
(`:101-109`), so every claim dies with its watcher and a restart retires the lot — the same restart
hole as the per-watcher `mktemp`, relocated. And `claim_disposition` (`:169-177`) maps `must-handle`
to 0, `fallback` to 1 and everything else to 2 = unknown, with only the first two reaching the
live-core branches, so a `direct` value is excluded by default. Both found by keweichen, checking a
claim I had made without checking it.

**The startup sweep obeys the same bound**, restated because an earlier revision of this
section dropped the sentence while rewriting around it: production loops every pre-existing
`tasks/*.txt` through `dispatch_task` at boot, so without this a restart carrying a backlog
claims and emits the whole of it before any ticker exists. Startup IS the first
reconciliation pass, bounded like every other, and its control uses a backlog larger than
the cap.

`2 *` is a starting point and should be tuned; what is load-bearing is that the bound is on
CLAIMS, is measured over total outstanding, and belongs to the ticker — because the event path
has no admission bound to inherit. A suppress/suppress pair therefore costs one beat of
latency instead of stranding the task, in step 2 and step 3 alike.

**It IS a scan, and an earlier revision claimed otherwise to make it sound cheaper.**
"Only tasks addressed to me" is not enumerable: `requested_worker` and the room id
live INSIDE flat task files, the pin is mutable, and suppression leaves no receipt —
so after a repin, no event tells the new target that an existing file now addresses
it. It can learn that only by looking. The same argument binds the CORE harder, and for a
different reason than an earlier revision gave: a dead worker emits no further beat, so
the core's tick must consider tasks addressed ELSEWHERE — not to claim them, but because
rule 6's verdict is defined over exactly those tasks (the oldest unclaimed task addressed
to that instance). The core scans other instances' work to WRITE the eligibility verdict;
it never takes it. No fallthrough to the core exists for a bound room.

### The reconciliation ticker

**It is a THIRD periodic mechanism, and it is gated on pool membership.** Two
passages had to change for that to be true rather than merely intended: "Workers are
task-only" named the heartbeat and the core's sweep as the only periodic things in a
pool, and the routing section still called this a per-heartbeat re-list after the
owner had moved. Both now name the watcher. It also supersedes Decision 5 of
[`core-pool-standing-sessions.md`](core-pool-standing-sessions.md), which sited the
unclaimed-work backstop on the LEAD and required followers to stay purely
event-driven — a placement this design cannot use, because the lead-as-daemon it
rests on is itself superseded at the top of this file. Decision 5's *cost* argument
is not superseded and is answered rather than dropped: it is O(N) wakeups, where N is
the number of workers deliberately created, and at N=0 there is no ticker at all.

**Activation.** The ticker exists only while the instance is a pool member. A default
install has no pool, so nothing arms it, and `## The starting point is zero workers`
keeps its guarantee literally rather than approximately. That gate is load-bearing,
not decorative: with no handler configured `DISPATCH_DIR` is empty and `dispatch_task`
prints `TASK_FILE:` unconditionally — no claim, no dedup, no probe
(`src/watch-tasks-stream.sh:370-376`) — so an ungated tick would re-emit every
still-pending file to the live core every 30 s, forever, on the majority
configuration. Deactivation is that edge in reverse: removing the last worker disarms
the ticker and the install is again exactly what it was.

**And "pool member" is not a new signal — it is the registry.** An earlier revision made
membership load-bearing without saying what reads it, which is a gate with no defined
input and cannot be implemented. The source of truth is the one `## Registry touchpoints`
already names: a worker registers `role: "worker"` and `pool: <name>` in its instance
manifest, and the core discovers workers *through the registry, never by scanning*. No
second file, no sentinel, no count cached anywhere — a membership record that can disagree
with the registry is the defect, not the mechanism.

**Who writes it:** the same commands that already create and remove workers. Creation
registers the manifest and starts the instance; removal deregisters and stops it. Nothing
else may write membership.

**How each watcher observes a transition, in both directions:**

- **A worker watcher is a member by construction.** Its own manifest carries `role:
  "worker"`, so it arms at boot and never needs to observe a transition — it stops being a
  member by being removed, which stops the process. There is no `1 -> 0` for a worker to see.
- **The core watcher reads the registry once at startup** and arms or does not. That alone
  settles every restart case in both directions, which is the case an implementation is most
  likely to get wrong.
- **A live `0 -> 1` is pushed, not polled**, and it has to be: a disarmed watcher has no tick
  on which to re-read anything, and giving it one would be a timer running at zero workers —
  precisely what `## The starting point is zero workers` forbids. So the command that creates
  worker 1 signals the core's watcher to arm, as part of the same act that starts worker 1.
- **A live `1 -> 0` is the same edge reversed**: the command that removes the last worker
  signals the core's watcher to disarm before it stops that worker.

**The registry does not carry this yet, and that fixes the staging order.**
`src/runtime-api/instance_registry.py` today has zero occurrences of `role` or `pool` and no
deregistration path at all (control: 51 occurrences of `instance`, so the file is the right
one). The watcher has no arm/disarm input either. So the reconciliation ticker CANNOT ship in
step 2 as the staged list has it: step 2 would ship a gate whose input does not exist, and a
gate with no input either never arms — leaving the zero-candidate race terminal — or is
implemented as "always on", which is the every-30-seconds re-emit this whole section exists to
prevent. **The membership machinery is therefore a prerequisite PR of its own, ahead of the
ticker:** the registry gains `role` and `pool`, gains a deregistration path, and the watcher
gains an arm/disarm input. The ticker ships after it, never beside it.

**And "signals the core's watcher" is not yet a specification.** It names no endpoint, no
acknowledgement or retry, and no ordering. The ordering is the part that decides correctness
and it goes one way only: **commit the registry change first, notify second.** Notify-then-commit
can arm a ticker for a pool that does not exist if the commit then fails, and the missed-signal
case is already covered by the startup re-read — so a lost notification costs latency until the
next restart, while a premature one costs duplicate delivery. The retry policy follows from the
same asymmetry: retry the ARM (recoverable, bounded by the next restart), and treat a failed
DISARM as an error that must be surfaced rather than retried silently, because the window it
leaves open is the expensive one.

**The fallback is fail-closed, and the direction is deliberate.** If a signal is ever missed,
the next watcher start re-reads the registry and converges; and a registry that is absent,
unreadable or unparseable reads as **not a member**, so the ticker does not arm. Both failure
directions are bad, but they are not equally bad: a ticker that fails to arm leaves the
zero-candidate race terminal until the next restart, while one that fails to disarm re-emits
every pending task every 30 s to a live core. The recoverable failure is the one to prefer.

**Supervision.** The ticker is owned by the watcher process and shares its lifetime,
rather than being a detached timer that can outlive it. If the watcher exits the
ticker goes with it — a separate process would reintroduce exactly the split that put
the first version of this work on the heartbeat, which had the timer and not the
capability.

**Single-flight.** One pass runs at a time. A tick arriving while the previous pass is
still walking the directory is DROPPED, not queued, because the next pass re-lists
from scratch and a coalesced tick therefore loses nothing. This is what stops a slow
`dispatch_task` from stacking passes that each re-emit the same files.

**Overrun.** A pass that outruns its period is a load signal, not an error. It is
bounded by the ticker's own admission bound, so it can never outrun what the instance
can run; a pass that hits the bound stops early and the next tick resumes from a fresh
listing rather than a saved cursor.

**Shutdown.** The ticker stops before the watcher stops claiming, so no pass is
mid-`dispatch_task` while the claim path tears down. An in-flight pass is abandoned
rather than drained: everything it would have emitted is still on disk, and the next
instance to arm a ticker re-lists it.

The cost is the one production already pays. `watch-tasks-stream.sh:639-645` lists
`tasks/*.txt` at every startup for exactly this reason — a restart gap leaves files
no event will re-announce — so the watcher's 30 s reconciliation is that same sweep on a timer,
not a new capability. It is bounded by the PENDING directory, which is small by
construction because tasks are consumed and archived promptly (measured on this host
while writing: 1 pending against 8,442 archived). If that ever stops holding, the
answer is an index, and the index is what this section would then owe. That IS a step-2 prerequisite, and
it is written into the step-2 list rather than asserted to be there — an earlier
revision asserted a listing it never made.

**Step 3 owns the wedge path, INSIDE THE SWEEP.** That answers the ownership question
the staging otherwise leaves open, and it is forced by how routing actually runs:
`SUTANDO_TASK_EVENT_HANDLER` is invoked as an EXTERNAL PROCESS per event
(`src/watch-tasks-stream.sh:370-383`), so a handler cannot see an in-memory value the
sweep computed — a design that has the core "read the verdict it just computed" is
not implementable in a per-event subprocess. So in step 3 the eligibility verdict
is made by the sweep, which holds the value it just wrote; the core's
event handler keeps routing on beats exactly as in step 2 and never consults the
record. Workers read the record to gate THEMSELVES. One reader per decision, and the
one party that could disagree with the file is the party that wrote it.

This paragraph is about the STEP-3 residue only, and an earlier revision let it
deny the step-2 contract as well. To be explicit, because the two sit close enough
to be read as one rule: **step 2 DOES owe a periodic re-evaluation** — the
watcher's 30s reconciliation specified above and again in the step-2 prerequisite
list — and running it on a timer the watcher owns rather than folding it into an
existing beat does not make repeated work non-periodic. What is bounded HERE
is a different race: a worker that
suppresses on a `wedged` verdict the core has not yet acted on waits until the
core's next SWEEP, which is step 3's and is one of the two periodic things
"Workers are task-only" already admits. Two residues, two owners, two mechanisms;
neither replaces the other.

**And a target checks its OWN verdict before claiming.** The eligibility rule above
tells every OTHER instance when to stop suppressing; on its own it leaves rules 1 and
2 letting the named worker, the pinned worker, a dedicated worker and the room's bound
instance claim unconditionally — so an instance the core has recorded `wedged` can still
win the claim and strand the task. So each branch below is read as: **act only if my own
beat is fresh AND the record says `eligible` or says nothing about me; a stale beat OR a
`wedged` verdict suppresses**, and the room's work then stays pending until the verdict is
reset or an owner re-binds the room. Note what "until that instance returns" cannot mean here:
an instance suppressing on its OWN verdict is the reason its oldest unclaimed task stays
unclaimed, which is the input that produced the verdict — so returning is not something the
instance can demonstrate from inside this gate. See "Clearing a wedged verdict". It does NOT
fall through to rule 3: the room is bound, and v1 has no second claimant for a bound room.

Suppressing is still strictly better than claiming, and that is the whole reason to gate
both halves of the predicate rather than one. A wedged instance that claims **strands** the
task — claimed, undeliverable, and invisible until someone goes looking. Suppressing leaves
it pending, which is visible and clears by the two paths that already exist. Gating on
`wedged` alone leaves the other half ungated, so a stale-beat named or pinned worker would
still claim unconditionally and strand the task in exactly the same way. A wedged instance
that claims nothing is unaffected — this only binds the one that is wedged and still
claiming, which is the case that hurts.

1. `requested_worker` names this instance: claim, then emit to this session.
   Names another instance whose beat is fresh: **suppress**.
   Names an instance whose beat is stale: the task stays PENDING — no instance,
   the core included, claims it.
2. No field, and the room addresses this worker — it is **pinned** to it, or
   this is a **dedicated** worker's own room: claim, then emit. Pinned to a set CONTAINING this
   worker: the same — claim, then emit, at any position; the per-task claim settles which member
   wins. Pinned to a set or instance this worker is NOT in: **suppress**
   — the core included. A pin is addressing, so the core is a non-target here
   exactly as a worker is, and rule 3 must not be reached. Pinned to an instance
   whose beat is stale: the pin is unclaimable, so the task stays PENDING. It does
   NOT fall through to rule 3 — rule 3 is for work addressed to nobody, and this
   work is addressed.
3. The task is addressed to NOBODY -- the room is UNBOUND, i.e. the pin table names no
   instance for it: the core emits and workers suppress. **This is a binding test, not a
   liveness test.** A room bound to a stale instance is bound, so it does NOT reach rule 3;
   its work stays pending under the no-stand-in rule. Reading rule 3 as "no LIVE instance"
   is what made the core a stand-in by the back door, and it required the opposite outcome
   from the same document.

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

The trace below reads beats directly because it models the step-2 window, where the
published record does not exist and every fresh-beat target is eligible by the
default above. Once step 3 lands, "fresh" in it means the published verdict; the
race it demonstrates is unchanged either way, because the claim — not the freshness
read — is what decides.

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
| 89.9s | worker-2 | my own beat is fresh; `requested_worker` names me | **wins** | yes |
| 90.1s | core | worker-2's beat now stale; the room is bound to worker-2 | **does not contend** | no |

Under v1 this is no longer a race, and the reason is worth stating exactly: the core
does not claim a bound room whatever the beat says, so the second claimant an earlier
revision arbitrated against does not exist. Either interleaving leaves at most one
contender. Had worker-2 suppressed instead — its own gate finding a stale beat or a
`wedged` verdict — the task would stay pending rather than pass to the core.

This is a SUBTRACTION from the claim's justification, not an addition to it. Trace A no
longer shows the claim is load-bearing, because nothing contends in it. Trace B does, and
carries that weight alone.

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

**A stale target does not hand the room to the core.** When a bound worker's beat ages
out, rule 3 is not reached: the room is bound, so its work stays pending until that
worker returns or an owner re-binds. The transition the table above schedules against
therefore changes who is waiting, never who claims.

In each schedule the task file is consumed at most once — by whichever instance holds the
claim, if any — and every loser leaves the file untouched. "At most" rather than "exactly"
is the honest form under v1: a bound room whose only eligible instance has suppressed
consumes nothing until it becomes eligible again, and that pending file is the visible,
recoverable state this design prefers to a stranded claim.

Rule 3 still selects the core, unchanged, for work addressed to nobody — an unbound room,
or an unreadable bindings file. That case never had a second claimant to race.

A repin is the one place the claim arbitrates target-against-target, and only for
the bounded window in which an outgoing and an incoming member both read themselves as bound.
Contention between members of ONE set is expected and settled by the per-task claim; what the
repin window adds is contention between a set being replaced and its replacement. Elsewhere the claim is still
load-bearing only in that repin window: with the stand-in gone, target-against-target is
the sole contention v1 has left.

A task is eligible to every member of the room's `instances` set, and the per-task claim
decides which member takes it — see the binding table. A later form of addressing (an app control, a room command, a
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

`state/pool/bindings.json`. One writer (the core) and
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

**Schema.** A room binds to an ordered **set** of instances, not to one name — one
worker per room cannot express a room served by a pair, and it cannot express which
rooms must be kept off the same worker:

```json
{
  "version": 1,
  "bindings": {"<room>": {"instances": ["<name>", "..."], "pinned": true}}
}
```

- **`instances` is an UNORDERED MEMBERSHIP SET. Every member is an equal claimant; position
  carries no meaning.** A worker claims a room's task if it reads itself anywhere in that room's
  set, and the per-task claim decides which member gets which task. There is no primary, no
  standby and no failover order, so a member going dead demotes nobody and promotes nobody: the
  remaining members simply keep claiming. **So the no-stand-in rule is quantified over the SET, and
  the quantifier is the whole rule: a room goes unserved only when EVERY bound member is
  ineligible, never when one is.** For `{W1 stale, W2 live}` W2 keeps claiming and the room is
  served; suppression applies to W1 alone. A per-member reading of that rule and this paragraph
  demand opposite outcomes for the same set, which is the contradiction two reviewers found at
  the same head. The set is rewritten by exactly ONE event, and death is
  not it: an **owner re-bind**, which `os.replace`s the file over the same single-writer, atomic
  path the table above specifies.

  **An earlier revision made this an ordered list — `instances[0]` was the binding and the rest a
  failover order — and the owner corrected it: *"they don't always have an order and not always
  failover. They can be unordered equal participants."*** The correction is recorded rather than
  silently applied, because it TRADES something a later reader must be able to see: with equal
  members, two of them can hold two DIFFERENT tasks for one room at the same time. The per-task
  claim stops two workers driving the same TASK; it does not stop two turns inside one ROOM. That
  concurrency is what a single bound instance was buying, and it is now accepted by choice. A set
  of one — the common case — has neither the cost nor the question.

  **What equal membership costs, measured rather than asserted.** Two members claiming two
  different tasks for one room each win their task-specific claim and then drive one room's
  context concurrently. Against the production claim: *different task keys, same room -> 0, 0*
  (both won), against a control *one group key -> 0, 1*. Serialising that would need group
  identity, group admission, group release and a multi-room story; a single bound
  instance needs none of it.

  **v1 does not repin a room to another worker at all, and that is what closes this class.**
  Three revisions tried to make an automatic worker-to-worker failover safe — first calling the
  window "benign", then a `generation`, then fencing by cause — and each left a residual
  check-then-act, because a time-based check cannot be made atomic in prose. The right move was
  not a fourth mechanism: it was to notice that **the lifecycle table already answers death**
  (`:999`) and the answer is not another worker.

  | event | what v1 does |
  |---|---|
  | a worker's beat goes stale past `stand_in_after_s` | **nothing claims for those rooms.** The core kickstarts the plist; the work stays pending until the worker returns or an owner re-binds |
  | the worker comes back | it re-reads the binding, finds itself still a member, and resumes. Nothing was reassigned, so nothing has to be revoked |
  | the owner wants a different worker on that room | an explicit re-bind, and the outgoing worker is **stopped by the supervisor that owns it** before the new binding is written — see "What stopping a worker actually takes" below, because `launchctl bootout` alone does NOT stop it. Responsive is not quiescent — an answer says it was alive when it answered, not that it will not claim next tick — so the enforcer is the process boundary, not the worker's cooperation |

  **What stopping a worker actually takes, and what it costs.** An earlier revision named
  `launchctl bootout` on the plist as the enforcer. Measured against the reference implementation at
  #3604's pinned `6c0b416e`, that is insufficient in three independent ways, and
  `scripts/uninstall-core-pool.sh` says so in its own header: *removing a core is three steps, not
  one*. Its `remove_core()` runs `bootout`, then `rm` on the plist, then `tmux kill-session`, then
  removes `state/cores/core-<N>.alive`. Each of the three extra steps closes a distinct hole:

  - the **persistent-form follower session outlives the wrapper**, so the outgoing worker is still
    running after `bootout` and can still claim against the room it was just unbound from — the
    original race, surviving the fence meant to close it;
  - the **plist remains installed**, and `kick-pool` revives any installed plist whose session is
    gone, so the fence can be undone by the pool's own liveness path without anyone acting;
  - a **stale beat file** keeps the lead assigning work to a worker that is no longer there.

  So the re-bind fence is: stop the wrapper, remove or disable the plist so `kick-pool` cannot
  revive it, end the tmux session, clear the beat — then write the new binding. **This fence is
  stated for worker-to-worker, and two other binding transitions need one too — see "Every
  transition fences its outgoing claimant" below.** Restarting the
  worker afterwards is `launchctl bootstrap`, NOT `kickstart`: `kickstart` targets an already-loaded
  job and cannot restore one `bootout` removed. Every `kickstart` elsewhere in this document
  describes reviving a worker whose plist is still loaded, which is a different operation from this
  one.

  **`bootstrap` alone does not undo either stop form, so the restore is the INVERSE of whichever
  one was used — the fence offers a choice and the restore must answer it.** Naming only
  `bootstrap` leaves the worker down under both branches, and the rooms whose process-wide outage
  is acknowledged above stay dark rather than clearing:

  | stop form used | why bare `bootstrap` fails | ordered restore |
  |---|---|---|
  | plist **disabled** | `disable` persists in the service database; a disabled label refuses to load, so `bootstrap` fails while the file is present and correct | `launchctl enable <service-target>`, then `bootstrap` |
  | plist **removed** | `bootstrap` consumes a service *path*; the file is gone, so there is nothing to load | re-render the plist to its destination, then `bootstrap` |

  The removed branch is the repo's existing idiom, not a new one: every installer here
  (`src/install-*-launchd.sh`) writes `$DEST` and only then runs `launchctl bootstrap "$DOMAIN"
  "$DEST"`. The disabled branch has no in-repo precedent because nothing here calls `disable` — which
  is the argument for preferring **remove** as the fence's default stop form, and for stating the
  `enable` step explicitly wherever an implementation chooses `disable` instead.

  **And state the scope honestly, because it is process-wide while a re-bind is per-room.** A worker
  may hold more than one room — nothing forbids that — so stopping it to re-bind ONE room also
  stops its other rooms and any work in flight for
  them. v1 takes that trade deliberately: those other rooms' bindings go pending under the same
  no-stand-in rule as everywhere else, and clear when the worker is bootstrapped back. The
  alternative — a room-scoped admission boundary that fences one binding without touching the
  process — is the right shape and is out of v1 scope, because it needs an admission check the
  worker consults per claim rather than a supervisor boundary.

  **Every transition fences its outgoing claimant, and for two of the three that claimant is
  the CORE.** The fence above stops a worker. It does not cover the other two ways a binding changes,
  and both have the same check-then-act shape this section spent three revisions removing:

  | transition | outgoing claimant | incoming claimant |
  |---|---|---|
  | re-bind R from W to V | worker W | worker V |
  | **first-pin** R (unbound) to W | **the core**, serving R under rule 3 | worker W |
  | **unpin** R from W | worker W | **the core**, under rule 3 |

  First-pin: the core reads R unbound and claims task X, is suspended, the owner pins R to W, W
  reads itself a member of R's set and claims task Y. Different task keys, same room — the per-task
  claims do not contend, so **both win**, which is the identical measurement this section already
  quotes (*0, 0*). Unpin is that race mirrored.

  **First-pin is NOT fenced. v1 accepts a bounded overlap and narrows it with a re-read after the
  claim.** Two earlier revisions each promised a fence here — first in-process, then by the re-read —
  and this header is the second one, kept as a summary after the paragraphs below had retracted it.
  A summary that outlives its own retraction is the more dangerous half: an implementer reads it and
  builds mutual exclusion that the documented interleaving does not provide. The first of those
  revisions said the
  process executing the pin and the process serving R under rule 3 are the same, so the core could
  fence itself by draining. **That premise is false on the Codex path**, and this document already
  contains the evidence: `task-notifier.sh` starts `watch-tasks-stream.sh` as a DETACHED background
  process (`os.setsid` then `execv`) while the owner command is typed into the interactive core. Two
  processes. Draining the core's work fences nothing the watcher is about to do, and the watcher can
  pass its routing read before the binding write and publish its claim after it.

  **v1 does not fence first-pin. It ACCEPTS a bounded overlap, and calling the rule below a fence
  was wrong.** The reviewer's objection is exact and I am recording it rather than answering it with
  a third mechanism: *the claim serializes only ONE task, while the defect is concurrency between
  DIFFERENT tasks in the same room.* The per-task hard link and the bindings write are two atomic
  operations, not one ordering point, so the losing interleaving survives — the core claims task X,
  re-reads R as unbound, pauses; the pin publishes R -> W; W claims task Y; both execute. My own
  sentence *"the claimant already holds the only claim for that task and finishes it"* **accepts**
  that state. A rule whose stated success case is the overlap is not fencing the overlap.

  Closing it properly needs a room- or binding-generation admission boundary validated at EXECUTION
  time — which is the token machinery this section says v1 does not have, and saying it a fourth way
  would not create it.

  **So v1 states the overlap as accepted, and this is consistent rather than a concession:** the
  owner's equal-members ruling already admits two members driving two different tasks in one room at
  once. First-pin's window is strictly smaller than that standing condition — one task, one
  transition, self-clearing as soon as the losing claimant finishes — so a design that accepts the
  larger case cannot coherently claim to fence the smaller one.

  What the rule below IS: a cheap **narrowing**, not a boundary. It removes the case where a claimant
  executes for a room it demonstrably no longer serves, using two atomic primitives visible to BOTH
  processes — the per-task claim (a hard link only one acquirer wins) and the bindings write
  (`os.replace`):

  > **Claim first, then RE-READ the bindings, and release without executing if the room is no longer
  > yours.** Any instance that acquires a claim for a task in room R re-reads `bindings.json` before
  > it does any work, and verifies it is still an eligible claimant for R — the core that it is still
  > unbound, a worker that it is still a member. If not, it releases the claim and suppresses.

  What it removes: a claimant that observes the pin before it starts work stands down instead of
  serving a room it no longer serves. **What it does NOT remove, stated so no reader has to derive
  it:** a claimant that wins its claim before the pin lands executes that task while the newly bound
  worker executes a different one. That is the accepted overlap above. It is bounded to the tasks
  already claimed at the moment of the pin, and it clears without intervention.

  It is not the generation/token this section rejects below — nothing is stamped, nothing validates a
  version — and that is precisely WHY it cannot be a fence. A token is what a fence at this boundary
  would require. Calling a re-read a fence was substituting the cheap thing for the thing that works.

  **Unpin takes the re-bind fence unchanged, because it IS a re-bind — to nobody.** The outgoing
  claimant is a separate process, so by this section's own argument the enforcer is the process
  boundary; there is no in-process shortcut available in that direction. Treating unpin as a
  lighter operation than re-bind is what left it unfenced.

  What each costs, in the direction decided: first-pin costs one extra bindings read per claim —
  paid by every claimant on every task, not only during a pin — and, in the racing case, one task
  claimed and released without work. It does NOT wait for the core to drain, which is what the
  superseded version bought at the price of a premise that does not hold. Unpin costs a worker stop, which
  is process-wide and therefore also stops W's OTHER rooms — the same trade the paragraph above
  states for re-bind, and it is worth saying out loud that an owner unpinning one room does not
  expect to pause three.

  **The two rows above once claimed they needed no fencing, then tried to buy one with a timer.
  Both were wrong, and the second is the more instructive.** The stale row said the core is a single
  arbiter so a second live claimant cannot arise; the rebind row said a responsive worker can simply
  be told to stop. Both are check-then-act, and this section's own measurement four paragraphs up
  says so: *different task keys, same room -> 0, 0* (both won). A worker samples its eligibility,
  passes, and claims task X while the core — having just crossed `stand_in_after_s` — claims task Y
  for the same room. The per-task claims never contend.

  The first repair was a durable admission record plus a wait: mark the worker revoked, wait one
  beat interval plus one claim window, then stand in. **A wall-clock interval is not a fence.** A
  worker reads its admission, is suspended past any interval you name (scheduling delay, I/O stall,
  a stopped process resuming), and claims afterwards. Calling an interval a bound does not make it
  one; the only thing that would is a generation or token the *claim path itself* validates at the
  ownership boundary — which is precisely the group-admission machinery this section exists to say
  v1 does not have.

  **So v1 does not stand in at all.** There is no second claimant to fence because there is never a
  second claimant: an ineligible worker's rooms simply stop admitting new work. This is the move the
  section already makes for worker-to-worker failover one level up — *the unsafe operation was
  removed rather than guarded* — finally applied to the core, which two earlier revisions kept
  exempting from their own argument.

  **What it costs, stated plainly, and it is larger than the previous revision claimed:** a dead
  worker's rooms stay unserved until that worker returns or a human re-binds. Not for a bounded
  interval — until someone acts. Work already claimed is still reclaimed behind the done flag, so
  nothing in flight is lost; what is given up is throughput, not correctness. A design that cannot
  say which of the two it is trading has not made the trade.

  So `instances` carries no owner-facing ordering either: it is a membership set, and a human
  deciding whom to re-bind to reads the pool status, not a position in this list. No generation,
  lease, drain protocol or admission record is required — because after this revision there is no
  operation left for one to guard.

  What this costs, stated plainly: a dead worker's rooms stay pending until an owner re-binds them
  or the worker returns. Nothing else serves them in the meantime — not a standby worker, and not
  the core. That is a liveness cost, not a correctness one, and v1 takes it deliberately: a second
  claimant for a bound room is the race this design exists to remove.
  True fan-out — and automatic failover with it — needs group identity, group admission, group
  release and a multi-room story, and remains out of scope.
- **Every entry ineligible is not an error**: the room's work stays pending until an entry becomes
  eligible again or an owner re-binds. This is NOT the unbound case — a room with no binding still
  falls to the core under rule 3. A binding exists to stop scatter; honouring it while every entry
  is ineligible costs latency, and the alternative costs a second claimant.
- **Absent and empty both route to the core, and neither is ever filled by handler
  affinity** — rooms bind only by an explicit pin, and the affinity file is retired and
  deleted rather than consulted. The distinction is a record of intent, not a routing
  rule: an absent room was never bound, an empty one was deliberately bound to nobody,
  and an owner command is the only thing that changes either.
- A legacy `"instance": "<name>"` string reads as a one-element `instances` for one
  release, so the retired affinity file can be migrated without a flag day.

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
| pin room R to worker W / to set {W…} · unpin room R | the core rewrites the pin table; workers read it on every claim. **Neither is a bare write** — a first-pin is NOT fenced — v1 accepts a bounded cross-task overlap and the claim-then-re-read rule only narrows it (the pin itself needs no drain), an unpin takes the full re-bind fence; see "Every transition fences its outgoing claimant" |
| spawn N · set model of W | the core runs the installer (`spawn-worker`) |
| resize to N (shrinking) · remove worker W | an ORDERED transition — see "Removing a worker" below — because the installer alone leaves the departing worker's bindings pointing at an instance that can never return |

What the app needs back is read-only: `state/pool-status.json` (workers, mode,
the core's status line), which the core writes and the bridge already pushes.

## Workers are task-only

A worker has no proactive loop. It starts its task watcher at boot, claims any
eligible task already on disk, and after that every claim and finish is
triggered by a watcher event. A pool has three periodic things: each process's
heartbeat, the core's sweep, and the watcher's 30s reconciliation — the last armed
only while the instance is a pool member, so a fresh install still has two. Its
contract is in **The reconciliation ticker** above. Activation, claim and finish do
not justify a timer of their own, so no `proactive-loop-pool` skill ships.

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
**Removing a worker is an ordered transition, not an installer call.** No-stand-in makes an
ineligible worker's rooms wait for it to return; a REMOVED worker never returns, so the same rule
would strand those rooms forever and `resize to 0` could not restore core-only mode. The
installer-only path at the command table has no step that rewrites a binding, which is exactly the
gap. So removing W runs in this order, and the order is the contract:

1. **FENCE W FIRST — the same fence the re-bind row uses**, and for the same reason: stop the
   wrapper, remove or disable the plist so `kick-pool` cannot revive it, end the tmux session, clear
   the beat. Only after this can W neither claim nor execute.
2. **Drain what it holds.** W's already-CLAIMED work is reclaimed behind the done flag, per rule 5 —
   the one reclamation no-stand-in has always permitted, and why `resize to 0` can promise that
   in-flight work is not lost. Safe only after step 1: reclaiming work a live W may still be running
   lets the reclaimed path and W drive the same task at once.
3. **REWRITE THE BINDINGS.** W is removed from every room's `instances`. A room left with no entries
   is UNBOUND, and unbound work reaches the core under rule 3 — the ordinary path, not a stand-in.
4. **Then** the installer records the removal.

**Marking W ineligible is NOT a substitute for step 1, and an earlier revision of this list made
exactly that mistake.** Eligibility is a value the worker READS before claiming (`:85-91`), so a W
that read `eligible` and then suspended can resume after the verdict flips and claim against its
stale view. A read-gated flag cannot fence a process that is already past the read; only stopping the
process can. That ordering also contradicted the re-bind fence above, which has always ended the
worker session BEFORE publishing a new binding — the two paths must not disagree about what stops a
worker.

Steps 3 and 4 in that order is the whole point: reversed, there is a window in which the plist is
gone and the bindings still name it. `resize to 0` is this transition applied to every worker, which
is what leaves every room unbound and the core serving them all — core-only mode, reached by rule 3
rather than by a stand-in exception.

**This does NOT weaken no-stand-in.** The core still never takes a BOUND room's work. Unbinding is an
explicit owner-commanded transition with a recorded rewrite; what rule 5 forbids is the core helping
itself to a room whose worker might still come back.

5. **No stand-in:** while a pinned worker is ineligible — beat stale, or wedged
   under rule 6 — new work for its rooms stays PENDING. The core does not claim
   it, and neither does any other worker. Work the ineligible worker had ALREADY
   CLAIMED may be reclaimed behind the done flag, so nothing in flight is lost;
   what is refused is ADMITTING NEW work on its behalf. A fresh beat alone does
   not restore eligibility; rule 6 is what says whether beating counts. The pin is
   not changed; nothing is loaned or re-bound.

   The room is therefore unserved until the worker returns or the owner
   explicitly redirects or cancels the work. That availability gap is deliberate:
   there is no second claimant to fence because there is never a second claimant.
6. **Busy is not hung.** A worker with a fresh beat and a claimed, unfinished
   task is busy, and the core leaves its rooms alone. A fresh-beat worker becomes
   INELIGIBLE only when the oldest unclaimed task ADDRESSED TO IT is
   older than `stand_in_after_s` (default 300) **and** the worker holds no
   claimed task, plus one sweep of grace after its last finish. "Addressed to
   it" is every route in the routing rule, not the pin alone: a
   `requested_worker` task, a task in a room whose `instances` set contains it, and a
   dedicated worker's own room. Scoping this to pinned rooms
   would leave the other three routes with a wedged target that is never
   ineligible, so rules 1 and 2 suppress on it forever. This is the claim-only
   form of #3604's claimed-load, busy-deferral cap and post-busy grace
   (`pool_lead.py:514-547`), so a long task never has the core answering the
   same room concurrently.

   **Clearing a wedged verdict — the reset is COMMANDED, and it has to be, because the
   verdict is otherwise absorbing.** Rule 6 publishes `wedged` from two inputs: the age of
   the oldest unclaimed task addressed to the worker, and the worker holding no claimed
   task. The verdict then makes that worker suppress on its own pin, so it cannot claim
   that task — which is what keeps the task unclaimed and the worker claim-less. Both
   inputs are frozen BY the verdict. A fresh beat does not move either one, and the rule
   above already refuses to treat beating as recovery for the measured reason that a hung
   session beats normally at zero throughput. So the next sweep sees identical inputs and
   republishes `wedged` forever: the room is permanently dark, with every individual rule
   behaving exactly as written.

   The reset therefore cannot be inferred; some actor must perform it. **`kick-pool` owns
   it.** Un-wedging the idle prompt and clearing the verdict are one transition, not two —
   a kick that leaves the verdict standing changes nothing observable, since the worker
   resumes and still suppresses. The kick deletes the instance's entry from the published
   eligibility record (making it absent, which rule 1's table already reads as eligible),
   and the next sweep re-evaluates from scratch.

   **This admits exactly one task's worth of risk, deliberately.** A worker still hung
   after the kick claims the admitted task and strands it — claimed, undeliverable — which
   is the harm the self-suppression gate above exists to prevent. It is bounded: one task,
   re-detected by the next sweep in one `stand_in_after_s` window, and visible as a claimed
   task whose worker holds no lease. That is strictly better than the alternative it
   replaces, where the harm is unbounded and silent. **State the direction plainly: this
   design prefers a bounded, re-detectable strand over a permanently dark room.**

   The kick is an owner action in v1, not something the core does on a timer. Automating it
   would put the core in a kick/re-wedge loop against a genuinely broken worker, and a loop
   that keeps admitting one task per window is a slow leak rather than a fix. Rate-limiting
   an automatic kick is a v2 question and is deliberately not answered here.

There is no election, no consensus and no degraded mode: the core is the only
process whose absence stops work, which is already true of every install today.

## Lifecycle: one owner, one trigger, one on-disk state per phase

| phase | owner | trigger | state left on disk / user-visible effect |
|---|---|---|---|
| create | core | owner command (`spawn N`) | one plist per worker, the config dir and model captured in it |
| start / stop one worker | launchd, revived by the core | `launchctl kickstart` (launchd defers non-demand spawns, so KeepAlive and timers do not bring a worker back) | on SIGTERM the worker unlinks its `.alive`, so its rooms go unserved at once |
| shutdown of the pool | core | owner command (`resize to 0`) | the ordered remove transition per worker — stop admitting, drain claimed work, REWRITE the bindings, then the installer; every room ends unbound and the core serves it under rule 3. Claimed-but-unfinished tasks are reclaimed by the core, never dropped |
| resume after host sleep | core | beats return | a host sleep is not N dead workers; the core waits for beats before reclaiming (#3782) |
| death of a worker | core | beat stale past the window | the core kickstarts the plist; its rooms stay pending until it returns or an owner re-binds. Claimed-but-unfinished work is reclaimed behind the done flag, so nothing in flight is lost |

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
working" is the **age of the oldest unclaimed task ADDRESSED TO a worker whose beat
is fresh** — by any route, matching §3 rule 6, not pinned rooms alone — which the
core computes in its sweep. Classifying a
worker that stopped, carried from #3604's operations section:

| symptom | class | action |
|---|---|---|
| auth errors, `401`, expired credentials | auth state, per process | recycle that worker (`launchctl kickstart -k`); a re-login elsewhere reaches only newly started processes |
| timeouts, 5xx | transport | back off and retry; do not touch the session |
| beat fresh, no claimed task, a pinned task unclaimed past `stand_in_after_s` | hung session | the room stops admitting new work. `kick-pool` un-wedges the idle prompt **and clears the `wedged` verdict in the same transition** — un-wedging alone does not resume the worker, because it suppresses on its own verdict. See "Clearing a wedged verdict" |
| beat fresh, credentials valid, every turn returns an out-of-credits error | **quota spent** | **quiesce** — the worker stops claiming until its window resets, and its rooms stay pending meanwhile. Kicking is worse than nothing: the seat still claims and then fails, so the task leaves the unclaimed state and never becomes visible as stuck. Measured live: four seats beating normally at zero throughput |
| beat fresh, a claimed task unfinished | busy | leave it; the room waits for its worker |
| no tmux session | dead | the core's reconcile kickstarts the plist |

**Quiesce is a state on disk, not a mood.** The table above prescribes quiescing a quota-spent
worker, and a prescription with no writer and no reset is why four seats beat normally at zero
throughput. The record is `state/pool/quiesced/<instance>.json`, written temp-plus-`os.replace`
like every other pool file:

```json
{"instance": "<name>", "observed_at": "<iso>", "until": "<iso>",
 "window": "5h|7d", "source": "<the provider error that was seen>"}
```

| | contract |
|---|---|
| writer | **the WRAPPER that owns the instance's tmux session — one named component, not "a process that makes no model calls" and not "sidecar or wrapper". Never the worker, never the core, and NOT `src/core_heartbeat.py`.** The rule it satisfies is that it makes no model calls, but a rule is not an owner; three revisions each named a different party in a different row and left the others standing. The rule it follows: *the reporter of a resource exhaustion must not depend on that resource.* Quota is per-ACCOUNT, so every model session on it — the core included — goes dark together; a core-written record fails in exactly the outage it exists to describe. Owner, 2026-09-05, on my second attempt: *"I don't think the core owns that either. When there's quota issue all the CLI sessions may stop responding."* Two earlier revisions got this wrong in the same shape — first the worker (because a local write needs no quota, true and beside the point), then the core (because the worker might be too broken, also true and beside the point). Both named a party that the outage can take with it |
| the three roles, kept apart | the SESSION hits the error and lets it surface in its output — that is the error appearing, not a report. The WRAPPER, which costs no quota and outlives the session it watches, turns that into the durable record. The CORE only READS it, as a peer that may itself be dark |
| the core's role | reads it, and nothing more. It does not take the room: a quiesced instance's bound rooms stay pending. (An earlier revision justified this row with "a worker too broken to write its own record is simply never eligible" — a leftover from when the WORKER was the writer. It is no longer a reason for anything and is removed rather than reworded) |
| the report transport, named — and the producer must be BUILT | **the WRAPPER that owns the tmux session, via `tmux pipe-pane` to a per-instance capture file.** One owner, not "sidecar or wrapper". **`src/core_heartbeat.py` does NOT do this today and the earlier revision was wrong to say it "already tails" anything** — it probes process/tmux metadata through `tmux_probe.classify` and writes `.alive`; it never captures pane output, and the `/tmp/core-heartbeat.log` the launchers create is the heartbeat's OWN stdout, not provider output. So this is a component to write, and it owes: the `pipe-pane` capture, provider-error attribution (which lines are an out-of-credits error rather than ordinary output), the parse that extracts the provider's reported reset time, and the atomic temp-plus-`os.replace` write. Nothing the failing session has to successfully DO — the pane already carries its output. A runtime with no pane to pipe has no quiesce detection, and the design says that rather than assuming one |
| routing exclusion | a quiesced instance is skipped at claim time (not "in `instances` order" — the set is unordered). A room whose every binding is quiesced stays pending. This is deliberately NOT the unreadable-bindings fall-through: an unreadable file leaves the room unbound, so rule 3 sends it to the core; a quiesced binding is still a binding |
| reset — SOLE authorized transition | **the WRAPPER that wrote it, and only that wrapper, unlinks it — the same component as the writer row, by construction.** An earlier revision assigned the unlink to the worker "on its first successful turn", which is absorbing in exactly the way §3 rule 6's wedged verdict was: a quiesced instance is excluded from claims by the row above, so it can never produce that turn, and the record clears only by expiring — at which point the unlink it was waiting for is moot. One writer, one deleter, same process. `until` is the provider's **own** reported reset time, never an estimate; a record whose `until` has passed reads as expired and the instance is eligible again, which is the fail-toward-eligibility below and NOT a substitute for the reset |
| attribution | `window` and `source` are what let an operator tell quota-spent from hung. Without them both look like "beat fresh, nothing claimed" |

**Expiry fails toward eligibility, deliberately.** A quiesce record that outlives its window strands a
seat forever, and a stranded seat is invisible — it beats, it is healthy, it simply never claims. So a
stale record is ignored rather than trusted. The cost of being wrong is one failed turn that
re-writes the record; the cost of the opposite default is a permanently dark worker nothing reports.

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

**Except for the watcher-integration PRs named below, the pool ships as new
files only**: the worker claim path and pin table, the core's sweep, installer
and plists, or as a skill. Apart from those PRs, existing Sutando files are not
edited except the index and test bookkeeping CI requires when new modules land
under `src/`.

The exception is not a footnote, so it is stated as part of the rule rather than
after it. Step 2's ticker is assigned to the watcher rather than to a new file,
so it necessarily edits `src/watch-tasks-stream.sh` — its initializer (a new
receipt namespace), its dispatch admission (the bound), its completion handling
(the release), its cleanup, and `claim_disposition` if the receipt is built on
the claim record. That is a direct change to the most load-bearing script in the
repo. Each such change is its own PR with its own reason and its own
production-path tests; the staged list below marks which those are.

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
   Its own PR against an existing file, and one of TWO left. The other is the
   **membership prerequisite**, which must land BEFORE the reconciliation ticker:
   `instance_registry.py` gains `role: "worker"` and `pool: <name>` on the manifest
   writer and a deregistration path (today it has neither — zero occurrences of
   either field, against 51 of `instance`), the create/remove commands gain the
   arm/disarm signal to the core's watcher under commit-then-notify ordering, and
   the watcher gains the input to receive it. Its suite pins the five activation
   edges the routing suite does not reach: core startup at zero workers, core
   startup at nonzero, live `0 -> 1`, live `1 -> 0`, a worker arming from its own
   manifest, and an unreadable registry failing closed to not-a-member. The
   remaining one: the watcher
   sentinel becomes per instance (`state/watch-tasks-stream-<name>.pid` and its
   four readers, since N watchers on one host overwrite the single file today)
   and the fallback-receipt directory with it. The sparrow bridge carrying
   `requested_worker` is **already done** by #3872 rather than pending —
   exercised through `_write_task`, the field is serialized above `task:` and
   the safe parser reads it back, so nothing further is owed there.
   **A second requirement on step 2's own code, and the one that keeps a
   suppress/suppress pair from stranding a task: the WATCHER re-lists the pending
   directory every 30 s and runs each file through `dispatch_task`**, the same path a
   Created event takes. Owned by the watcher and not the heartbeat, because
   `core_heartbeat.py` is a detached liveness sidecar with no reference to
   `dispatch_task`, `acquire_task_claim` or `TASK_FILE`; the watcher already lists
   `tasks/*.txt` at startup and already holds the claim, so this is that sweep on a
   timer rather than a new capability or a second claim recipe. It is a full re-list,
   not a filtered walk of "tasks addressed to me": that set is not enumerable without
   looking, since the fields that address a task live inside the file and the pin can
   change after the file lands. **No age gate** — see the routing section for why the
   "older than one beat" form costs a second tick, inverts priority against a fresher
   urgent task, and leaves future mtimes undefined, while buying nothing the claim does
   not already do. Order is the queue's priority order; one tick claims everything this
   instance is the candidate for, under the ticker's own admission bound (there is no
   event-path admission bound to inherit).
   Production scans once at startup and then reacts to Created/Renamed events only
   (`src/watch-tasks-stream.sh:639-702`), and suppression creates neither, so without
   this every zero-candidate schedule is terminal. Its suite ALSO pins a
   direct-emitter backlog (a direct admission writes a `direct/` receipt and is still
   counted on the NEXT pass, which is the case a per-pass counter loses), a queued
   winner counted ONCE rather than by both its marker and a second tally, a LOST claim
   consuming no quota (production reports `lost`; the caller cannot infer it, since
   `queue_handler_task` returns 0 either way), a MIXED pass carrying both a queued and a
   direct admission — neither single-route case can detect an ASYMMETRY between the two,
   which is exactly what a second tally beside the markers would introduce — a no-handler
   start (empty `DISPATCH_DIR` arms no ticker), and a Created event interleaved with a pass. It pins BOTH reverse orders —
   the beat crossing stale between the core's read and the target's, and the pin
   swapping between two workers' reads — plus a repin-after-suppress case where no event
   is generated at all, and one asserting the reconciliation claims through
   `dispatch_task` rather than a path of its own.
   **Three more pin the admission bound, which the routing cases do not reach:**
   a pass beginning with outstanding ALREADY at the bound admits nothing (the
   pre-existing running/pending backlog counts, so the bound is not a fresh
   per-pass allowance); a pass cut short by the bound admits in the queue's
   PRIORITY order rather than directory order, so the cutoff never strands an
   urgent task behind an older low-priority one; and the remainder is admitted on
   a LATER tick once outstanding drops, from a fresh listing rather than a saved
   cursor — which is also the control proving an early stop loses nothing.

   **Not a prerequisite PR, a
   requirement on step 2's own code:** the eligibility reader matches both
   `channel_id` and `chat_id`. No such reader exists on `main` — checked, it
   has never been there — so there is nothing to fix ahead of time; the failure
   is a Telegram-addressed task silently reading as unbound the first time
   someone pins a chat, and step 2's suite carries that case.
3. core side: the pin table writer, the sweep (reclaim, revive, status line,
   timing record — no stand-in), with claim and liveness tests; **and the worker
   side of the same step: the eligibility reader and its self-gate, which ship
   WITH the publisher rather than ahead of it** — a reader that lands first is a
   consumer with no producer, and one that never lands leaves the sweep publishing
   a verdict nothing obeys;
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

## What the retraction tests do and do not show

The retraction suite validates **document evolution only**: it shows that wording this design
specifically retracted has not come back. It does not demonstrate that the production mechanism
exists, or that it behaves correctly. A green suite is progress toward coherence, not toward merge,
and the two read identically from a CI badge.

`ChosenContractIsPinned` narrows that gap without closing it. It asserts the chosen owner/outcome
contract is present and its superseded counterpart is asserted nowhere — which catches an obsolete
model left standing beside its replacement, the failure the phrase checks structurally cannot see,
because nothing has been re-worded. It remains a **document-level data pin**. It does not prove
semantic consistency, and it does not prevent two incompatible rules from coexisting so long as
neither uses retracted wording.

Three statuses therefore stay distinct, and none of them implies another:

| status | what it means |
|---|---|
| the admission design | **design-complete** — the three phases, the five outcomes and the three crash windows are settled |
| semantic-consistency enforcement | **open** — it needs stable IDs on the normative rules and a structured mutually-exclusive-state table that can be tested directly |
| runtime implementation proof | **a separate future gate** — see the acceptance bar below |

Implementation acceptance requires all of:

- one shared production admission/transition primitive, not a per-caller reimplementation
- coverage of **both** the event and reconcile lock orders, exercised against production code
- every direct branch, including the typed refusal in the `fallback`-mode publish branch
  (`watch-tasks-stream.sh:390` today; the mode argument is the identity, the line is a locator)
- fault injection for all three crash windows (`receipt-before-emit`, `emit-before-ack`,
  `ack-before-release`)

None of that is in this PR, and no test in it should be read as evidence for any of it.


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
