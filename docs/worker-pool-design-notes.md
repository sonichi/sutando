# Worker pool — design notes (the record behind v1)

This file is the record. [`worker-pool-design.md`](worker-pool-design.md) is the
contract; nothing here is normative, and where the two disagree that file wins.

What is kept here: why each rejected shape was rejected, the measurements that
forced the choice, the owner decisions in their own words, and what this design
supersedes.

## Why the record is a separate file

The #3860 draft carried its own history inside its normative sections, and the
cost is measurable rather than stylistic. At head `d2e41ace3` that draft is 1,914
lines and needs a 1,225-line retraction test to guard it — 64% of the document it
protects — because a rule and the account of how the rule got there sit in the
same paragraph, so a reader cannot tell which sentence binds an implementer. As
supporting detail, the same head carries 38 lines matching `earlier revision |
retracted | superseded | previous paragraph | two reviewers`, and a dedicated
section titled "What the retraction tests do and do not show" whose whole purpose
is to say that a green suite proves document evolution and not implementability.

Splitting the two makes each testable in the way it can be: the normative file is
tested for *absence* of history and of every rejected mechanism, and this file is
free to carry the history at whatever length it is worth.

## Why not a lead inside the runtime daemon

`docs/lead-follower-pool.md` (on `origin/rescue/pool-uncommitted-2026-08-26`,
whose head merged `origin/main` at `b6cc5da83`) places the lead inside the
runtime daemon at `src/runtime-api/server.py`, and its stated reason is that the
daemon is a single admission point.

Measured against #3604 at `6c0b416e`: `server.py` has zero references to the
lead, the daemon is not started by any launcher on this host, and the single
admission point the placement was justified by was never built. That is not an
argument against a single admission point — it is the argument *for* building one
as its own process, which is what the pool supervisor is.

The second reason is architectural rather than archaeological. The runtime daemon
is transport plus daemon composition; CLAUDE.md's "Transport vs request domain"
rule already keeps dispatch, authorization and durable transitions out of it. A
scheduler is durable-transition work by definition, so it does not belong there
either.

## Why the core must not be the supervisor

This is the load-bearing correction, and the evidence is internal to the #3860
draft rather than external to it.

**The draft's own invariant is false against its own table.** At `d2e41ace3`, the
"Who does what" table gives the core row: create, destroy and resize the pool;
choose a worker's model; execute every lifecycle command; write the pin table;
sweep worker beats, reclaim, revive, report — *and* claim every task addressed to
no worker at all. Immediately under that table the draft states, at L55: **"No
component does two of these."** The core row is control plane and data plane in
one row, so the invariant the draft asserts is contradicted by the table it
asserts it about. One of the two has to give, and the table is what the rest of
the draft implements.

**The draft already rules the core out on the correct principle, one row
earlier.** Its quiescence section refuses to let the core write the quota-spent
record, and states the reason as a principle: *the reporter of a resource
exhaustion must not depend on that resource*. The chosen writer is the wrapper
that owns the instance's tmux session, because it "costs no quota". The owner's
own words there, 2026-09-05:

> "I don't think the core owns that either. When there's quota issue all the CLI
> sessions may stop responding."

Quota is per account. If every CLI session may stop responding together, then a
control plane sited in one of them stops responding in exactly the outage it
exists to act in — an owner cannot resize, re-pin, kick, or even be told what is
wrong. This design applies that same principle one row further: not only the
*reporter* of exhaustion but the *scheduler* must not depend on the LLM session.
That is a consistent extension of a ruling the draft had already made, not a new
opinion about it.

## The suppress/suppress counterexample

The #3860 draft's routing rule is evaluated independently by every watcher, and
each watcher suppresses when it reads itself as a non-target. The draft found its
own counterexample at L219-221:

> the core reads the old snapshot (`eligible`, so it suppresses as a non-target)
> against the worker reading the new one (`wedged`, so it suppresses too) gives
> **zero**, and suppression leaves the task file untouched.

Both reads are valid and neither instance is wrong; they simply sampled a
changing record at different instants. The task strands, and because suppression
writes nothing, no file event exists for anything to react to.

The draft's answer was a third periodic mechanism — a 30-second reconciliation
ticker inside each watcher, which then needed an admission bound of its own,
which then needed a durable receipt, which then needed a receipt phase, a lease,
and a three-window crash contract. Every one of those is a correct response to the
previous one. The chain exists because the hole is real.

**A single supervisor cannot produce the schedule.** The zero-candidate outcome is
a property of *distributed* suppression: N parties, N independent samples of one
changing state, and a decline that leaves no trace. One party that reads the state
once, decides a target, takes the lease in the same transaction, and notifies that
target has no interleaving in which every candidate declines — there is only one
candidate-picker. This is why the normative design's periodic reconciliation is
scoped to lease expiry and restart convergence: it is not repairing a routing
hole, because there is no routing hole for it to repair.

## The claim/accept deadlock

The draft measured a second defect that is worth carrying forward as a
requirement rather than as history, because the supervisor design must not
reintroduce it.

Against the shipped Codex consumer: `next_pending_task` in
`src/agent/codex/cli/task-notifier.sh` skips any candidate that has a claim file,
unconditionally, and before it consults anything else. `TASK_FILE:` on stdout is
only a wake — the loop rescans the whole queue through that same function, and
there is no other trigger. And `src/watch-tasks-stream.sh` persists and emits
*before* releasing, deliberately, on the stated ground that duplicate delivery is
acceptable while release-before-emit could permanently strand work.

Compose the three and the direct path emits a wake its own consumer is required
to refuse: the claim is present at wake time, the candidate is skipped, and the
later release fires no second event. The task is durable and unsubmitted — worse
than lost, because every surface reports it as pending.

The draft's fix was a second hard-link directory,
`state/task-event-handler-accepts/<canonical-id>`, plus a migration of the
consumer's skip rule and its key at the same time.

**What the normative design takes from this:** delivery is not admission. An
executor must explicitly accept, and the accept is a store transition rather than
a file whose presence has to be interpreted. An offer that is delivered and never
accepted expires with its lease and is re-offered, so the deadlock's failure mode
— durable, unsubmitted, invisible — is not reachable.

## The rejected alternative: a file protocol without transactions

The draft's control state is a set of filesystem paths under `state/`:
`pool-status.json`; `task-event-handler-claims/<task-id>`;
`task-event-handler-accepts/<canonical-id>`; `pool-probation/<instance>` with its
`<instance>.admit/` sub-phases (`token`, `held/<task_id>`, `claimed/<task_id>`,
and a `spent` marker); `pool/bindings.json`; `pool/quiesced/<instance>.json`;
`cores/core-<N>.alive` and `cores/channel-<room-id>.handler`; and
`watch-tasks-stream-<name>.pid`.

The count is not the problem. The problem is that a multi-file state change has no
transaction, so every seam between two writes needs its own prose rule, and each
rule has to be argued rather than enforced. The probation allowance is the
clearest instance, and it is the one to cite.

Consuming one admit token is: `create(<instance>.admit/spent, O_EXCL)`, then
`mkdir -p held/ claimed/`, then `rename(token, held/<task_id>)`, then a later
rename of `held/<task_id>` into `claimed/<task_id>`. Recovery cannot walk that
directory, because the worker can promote `held/` into `claimed/` between the
sweep's two reads and the scan then finds nothing although the allowance was
present throughout. So recovery must ask two single-name questions instead — and
those two are not order-independent either. The draft mandates `stat(token)`
first, then `stat(spent)`, and shows the schedule that separates them:

```
read order          worker before   between   after
spent then token    ok              MINTS!    ok
token then spent    ok              ok        ok
```

That table is correct, and it is also the point: an ordering rule between two
`stat` calls is doing the work a transaction does for free. A crash between the
`mkdir` and the `token` write leaves an empty directory that matches none of the
three clocks, so the verdict can never end — an existence test standing in for a
state test, one layer down from where the same defect was already fixed.

Every one of those seams is a single conditional `UPDATE` in the store design.
`spent`, `token`, `held/`, `claimed/`, the claim directory, the accept directory
and the receipt phases have no counterpart in the normative file, and none should
be reintroduced.

## The crash-window analysis, carried forward as the failure model

The draft's most durable contribution is its enumeration of the seams. It names
three windows on the direct path — `receipt-before-emit`, `emit-before-ack`,
`ack-before-release` — and insists they are distinct states rather than one state
described three ways, because collapsing the last two loses the difference
between "nobody has been told" and "everybody has been told and the bookkeeping is
half-done", which need opposite recoveries.

It also establishes, by measurement on the production `dispatch_task`, that the
task file's *existence* cannot discriminate them: the file is written before the
notification, so it is present on both sides of the emit
(`before emit: task_present=True` / `after emit: task_present=True`, with the
bytes identical). The draft's conclusion — branch on the task's claim state, never
on the file's existence — is right and survives into this design as: branch on the
stored row, never on the filesystem.

The normative failure matrix is that enumeration re-expressed, with a fourth
window added because the store makes it visible:

| draft window | normative window | store state |
|---|---|---|
| `receipt-before-emit` | offer-before-delivery | `OFFERED`, lease expired, `accepted_at` NULL |
| `emit-before-ack` | delivery-before-accept | `OFFERED`, lease expired, `accepted_at` NULL, offer delivered |
| — | accept-before-completion | `ACCEPTED` or `RUNNING`, lease expired |
| `ack-before-release` | completion-before-release | `RUNNING`, lease expired, result present on disk |

The first two share a row state and are distinguished only by whether delivery
happened — which is exactly why the recovery for both is the same transition, and
why the design does not need to tell them apart.

## Owner decisions, 2026-09-03 (PR-triage room)

Quoted in #3860's PR body; each is carried into the normative file unchanged in
substance.

- "workers should be task only and the proactive loop should be disabled on them
  by default. I suggest removing them completely in the first PR to merge to
  main."
- "isn't the router responsible for routing? why should a non-router write the
  header to specify the worker?"
- "if we keep the implementation we should use the right terms."
- "I request for the policy to be simpler for the first PR to merge to main: only
  route to a worker when it's obvious to; otherwise route to the core."
- "initially there's no worker. The core can create workers etc. One thing to
  think about is what can the router do"
- "The user intent to create a worker may come from the ag2space app"
- "shall the design doc cover the worker management like creation / start /
  shutdown / resume?"
- "The PR is too large and complex. Shall we use it as a reference and stage PRs
  to target main? Start from a design doc"

The one decision from that day that this design **reverses** is the 2026-09-03
update recorded at the end of the same PR body: *"v1 has no router process …
the router's only remaining duties are liveness, which the core owns."* That is
the placement the 2026-09-07 critique overturns, for the reason in **Why the core
must not be the supervisor**. Every other decision above stands.

## Owner decisions, 2026-09-07 (Pro-Main room)

On holding the pin-fallback commits:

> "pin fallback：直接落在 reviewer 明确要求移除的 per-worker proactive loop 路径里，存在方向性冲突。"

and, in English in the same message:

> "Hold the pin-fallback commits for now. Since that code sits in the per-worker
> proactive loop that #3604 has been asked to remove, merging it into #3604 would
> work against the requested reshape even if the implementation itself is correct.
> After the loop is removed, we should reassess whether pin affinity still needs
> enforcement and, if so, move it to the surviving centralized claim/assignment
> boundary rather than reintroducing it through the old fallback path."

The decision rule that follows, quoted verbatim:

> "如果新架构仍可能由多个 seat 抢同一任务，就把 pin guard 移到统一的 claim/assignment
> 层；如果新的 server-side routing 已经保证唯一 seat，则不应继续保留 follower-loop
> fallback。"

English rendering: *if the new architecture can still have several seats
contending for one task, move the pin guard to the unified claim/assignment
layer; if the new server-side routing already guarantees a unique seat, the
follower-loop fallback should not be kept.*

**How this design answers it.** The second branch applies. One pool supervisor
routes each task once and takes the lease in the same transaction, so exactly one
seat is ever offered a given task; there is no contention for a pin guard to
arbitrate. Therefore no fallback survives in any executor, and pin enforcement is
not a guard at all — it is the routing table, evaluated in the one place that
assigns.

The critique closes with the one-sentence adjustment this design is built around, quoted verbatim:

> 不要让 N 个 watcher 通过 suppress、claim、receipt、accept 和 ticker 共同“涌现”出路由结果；让一个不依赖任何 LLM session 的 Sutando supervisor 明确决定路由，Claude/Codex session 只负责接受和执行任务。

In English: do not let N watchers make the routing outcome "emerge" from suppress, claim, receipt, accept and ticker together; let one Sutando supervisor that depends on no LLM session decide routing explicitly, and let the Claude/Codex sessions only accept and execute tasks.

## The 2026-09-07 identity measurement

A task's on-disk filename is transient: written as `task-<id>.txt`, renamed while
in flight, archived under the canonical name. A classifier that derived identity
from `path.stem` accumulated **254 store rows under names that no longer exist**,
including a non-numeric `claimed-core-legacy` suffix.

Two normative rules follow, and both are in the contract file: derive identity
from the `id:` header through the shared resolver (`task_archive.task_id_for`),
never from a filename; and never assume a worker identifier is numeric.

This measurement also confirms the direction the #3860 draft had already reached
on its own: its coordination contract keys the claim on the canonical task id
rather than the basename, on the ground that the lifecycle rename it prescribes
would otherwise hand the renamed file a key nobody holds.

## What this supersedes

- **#3860 at head `d2e41ace3`** — the draft of this same v1. Its architecture
  (core as both executor and control plane; per-watcher routing with suppression;
  a filesystem protocol of claims, accepts, receipts and probation tokens) is
  replaced by the pool supervisor, the transactional store and the explicit
  accept. Its vocabulary decisions, its routing decisions and its crash-window
  enumeration are carried forward.
- **`docs/lead-follower-pool.md`** — the lead-inside-the-runtime-daemon placement
  and the lead-managed assignment it rests on are replaced by the pool supervisor
  as its own process.
- **Decision 4 of `docs/core-pool-standing-sessions.md`** ("fixed N is replaced by
  lead-managed sizing") — sizing is an owner command executed by the supervisor;
  no component derives N for itself.

Decisions 1, 2, 3 and 5 of `core-pool-standing-sessions.md` are not addressed by
this design and are neither restated nor relied on here. A later revision that
wants Decision 3's per-context-group guarantee must build group identity, group
admission and group release; the normative file lists all three as out of scope.

## Out of scope for this record

The stand-card dashboard, and any auto-scale or second scheduler process. They
are listed in the normative file's out-of-scope section, and nothing here argues
for or against them.
