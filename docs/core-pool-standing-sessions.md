# Core pool + standing sessions: how they compose

Design record for the discussion in the AG2 Space `Sessions` room, 2026-08-25.
Nothing here is implemented. It records decisions and the reasoning that
produced them, so the next change does not re-derive them from scratch.

Context: #3314 (lead-follower pool), #3263 (standing sessions).

## The tension, stated first

Followers are interchangeable **by design** — that is what makes least-loaded
fallback work. Standing sessions are deliberately **not** interchangeable — that
is what makes thread continuity work.

The naive composition ("every follower is a standing session") collapses the
lead's fallback into affinity-only: a room's task can go to exactly one follower
even with N-1 idle. That is paying for a pool and getting a static assignment
table.

## Decision 1 — a follower is a standing PROCESS, not a standing CONVERSATION

Measured on #3314's head, `src/runtime-api/pool_lead.py`:

```
AFFINITY_IDLE_S   = 30 * 60
AFFINITY_BUSY_MAX = 3   # outstanding assigned+claimed before affinity yields
```

Affinity expires after 30 minutes idle **and yields once a follower holds 3
outstanding items**. So a room's next task moves to a different follower
precisely when that room is busy — which is when continuity matters most.

Idle, a follower does behave like a standing session for a channel. Under load
it does not. A "standing session" that relocates under load is not one, so the
equivalence fails at the busy case, not the idle one.

Affinity is a correct scheduling heuristic. It is not a continuity guarantee,
and #3314 does not claim it is.

## Decision 2 — the binding unit is a CONTEXT GROUP, not a room

Relevant context spans rooms. So the lead holds a record keying a *set* of rooms
to a core, with a single room as the degenerate case.

This also removes the only judgement call in the create policy. "No existing
core has relevant context" is not mechanically measurable in general — but under
grouping it collapses to a lookup: *does this room belong to a group that is
already seated?* That keeps the lead model-free, which is the property #3314 is
built on and the one most worth not spending.

**The grouping is declared, not inferred.** An AG2 Space room already has a
`separate sessions` setting, and it carries exactly the right semantics: a room
with it on is its own group and wants a dedicated core; rooms without it may be
grouped. An existing user-facing knob beats a heuristic.

## Decision 3 — pane/session ownership sits OUTSIDE the follower

The session pane keyed `(runtime, room-group)` is a **resource the pool
schedules against**, not a property of a follower.

The lead assigns a task to whichever core is seated for that group; that core
attaches to the group's existing pane, takes the turn, and releases it. Each
side keeps its property: the pool keeps distribution across groups, standing
sessions keep continuity within one.

The constraint that makes this work: **a follower's identity is deliberately
fungible** (`AFFINITY_BUSY_MAX` is the code that makes it so). Bind a pane's
lifetime to a follower and a fungible unit becomes non-fungible; unpicking that
later is the expensive direction.

Two workers must never drive one group's pane at once — which is a lock, and
#3314's atomic-rename assignment already is one. Extend the assignment key from
*task* to *task + group-session lease*.

### Where the lease lives — two structures, two lifetimes

Raised in review: does the group-session lease ride the assignment filename, or
the lead-held binding record? Both readings were live in an earlier draft. The
answer is below, and it is NOT the one this section originally reached: the task
lease is free, the GROUP exclusion is not, and conflating them is what made an
earlier draft claim decision 3 cost nothing.

They are **different objects and must not be merged**:

```
task lease      assignment FILENAME     dies with the assignment
                task-X.assigned-<inst>  reclaim_dead()'s rename already releases it
                                        -> free FOR THE TASK ONLY; it grants no
                                        exclusion over the task's GROUP

group binding   lead-held RECORD        deliberately OUTLIVES the core
                                        restore-after-preempt requires exactly this
                                        -> reclaim_dead does NOT and must not touch it
```

**Correction (review, 2026-08-28): Decision 3 is NOT free, and the paragraph
this replaces was wrong.** It claimed the existing rename "expresses that
correctly and for free". It does not. #3314 assigns by renaming
`tasks/task-X.txt` -> `tasks/task-X.assigned-<instance>.txt` — the key is
**task-X**. That rename is atomic *about that task* and says nothing about any
other object. Two tasks belonging to the same context group therefore both
rename successfully, and two followers drive one pane concurrently. Per-object
atomicity on X is not mutual exclusion over Y; treating it as such is the whole
defect.

**The mechanism: lead serialization, because the lead is already the sole
assigner.** #3314 states it outright — *"A follower executes only work the lead
assigned to it"*, and the lead performs the rename. So exclusion does not need a
second lock; it needs the lead to refuse to create the second assignment:

> **At most one outstanding assignment per context group.** Before assigning
> task-X to group G, the lead requires that G has no assignment outstanding.

**Make group-busy derivable, not stored.** Extend the assignment filename to
carry the group:

```
tasks/task-X.assigned-<instance>.g-<group>.txt
```

Group-busy is then a glob, re-derived from the same files
that ARE the assignments. **Anchor it on the assignment segment:**

```
*.assigned-*.g-<group>.txt          <- use this
*.g-<group>.txt                     <- see the truth table below; it is not
                                       wrong for THIS layout, it is wrong for
                                       the layout someone might switch to
```

Truth table over both layouts, since "unrepresentable" is a strong claim:

```
                     *.assigned-*.g-G.txt   *.g-G.txt
A assigned                   True              True
A after reclaim              False             False
B assigned                   False             False
B after reclaim              False             True     <- permanent-busy
```

**The permanent-busy case is layout B's, not layout A's** — under A the reclaimed
name is `task-X.txt` and neither glob matches it. So anchoring is not protecting
the layout this document adopts against its own reclaim; it is making layout B's
failure unrepresentable, and buying one more property: the anchored form also
refuses layout B's *assigned* name, so B cannot be silently adopted later by
someone editing only the filename. The glob stops matching rather than matching
the wrong thing.

**`<group>` needs a typed key constructor, not string interpolation.** Group ids
are room ids like `!cAAaogVSFcIYaNmohw:ag2.space` — `pool_lead.py` already notes
at its filename regex that "ids legitimately contain dots". This repo mandates
exactly this shape for `phoneCallKey` / `phone_call_key`; the group token needs
its own, so writer and glob cannot disagree about where the segment ends. No second store can fall out of sync with them, and a
lead restart re-derives the busy set from disk rather than trusting memory it no
longer has.

**Crash and reclaim semantics, which the previous text never stated:**

| event | task lease | group exclusion |
|---|---|---|
| follower dies mid-task | `reclaim_dead()` renames back | released, because the glob no longer matches |
| task completes | assignment file consumed | released by the same disappearance |
| **lead** dies holding no file state | unaffected | re-derived on restart from the glob |

The group is released by exactly the event that releases the task. **The
accurate claim is therefore narrow: no group-specific stranding beyond what the
task lease already has.** Coupling INHERITS the task lease's liveness; it does
not improve on it, and an earlier draft of this section overclaimed "no path".

The composition that falsifies the absolute (review, 2026-08-28): `reclaim_dead()`
is not unconditional — it opens `if self._advance_reclaim_guard(): return []`,
and that guard arms a `LEAD_STALE_S` (90 s) window on host-sleep skew or on a cold
lead whose followers have not re-beaten. So rows 1 and 3 compose: a follower
holding G dies, the lead restarts, an unproven follower arms the window, the
rename never happens, and the glob reads G busy with nothing running for up to
90 s. Each row is true alone; the sequence is what the table did not cover.

Bounded, and the guard is right to exist — but it is the task lease's bound, which
is the whole point of the narrower claim.

**The alternative I did not take**, for the record: a distinct group-scoped
atomic object (`group-<G>.lease-<inst>`, acquired before executing any task in
G). It is more robust to multiple assigners, and correspondingly it is the right
choice only if the single-assigner property above ever stops holding. It costs a
second lock with its own reclaim path.

**Lead serialization does NOT cost "a filename suffix" — that comparison was
wrong, and it is the one selecting this design, so it has to be checkable.**
Tested against #3314's three live parsers, the suffix does not work at either
position without editing them:

```
task-X.assigned-<inst>.g-<group>.txt   the reclaim regex captures the instance
                                       as (.+) — GREEDY — so it swallows
                                       ".g-<group>" and asks alive_fn() about a
                                       name with no .alive file. Reads DEAD, and
                                       reclaims assignments from LIVE followers
                                       mid-task: the exact failure this decision
                                       exists to prevent, via its own mechanism.
                                       _load() anchors on \.assigned-<inst>\.txt$
                                       and matches nothing, so every follower
                                       reports load 0 and AFFINITY_BUSY_MAX never
                                       trips.

task-X.g-<group>.assigned-<inst>.txt   instance parses, but the rename target
                                       task-X.g-<group>.txt still matches an
                                       unanchored busy glob — permanently busy,
                                       unbounded.
```

**True cost: the reclaim regex, the `_load` pattern, and an anchored busy-glob.**
The decision still survives that — it is cheaper than a second lock with its own
reclaim path — but the reader has to be able to check it rather than take it.

If #3314 ever admits a second assigner, this decision must be revisited
rather than patched.

The **binding** cannot use the same mechanism, because decision 4 requires it to
survive the core — a lease that dies with its holder is the one thing restore
must not have. So the binding needs its own liveness, and it is not a rename:
it is the lead observing that a binding's occupant is gone and re-seating it.
That is cost #3 below, and it is the reason cost #3 exists rather than being
folded into reclaim.

The failure mode if the two are conflated: a core dying mid-turn releases its
task (correct) and also drops its binding (wrong — the group is now unseated with
no record of where it belonged), or the binding self-expires on a timer and a
preempted group is silently re-seated somewhere else while restore still thinks
it owns it.

## Decision 4 — fixed N is replaced by lead-managed sizing

The lead already never holds an `N`. It takes its fleet as injected callables
and re-derives liveness per assignment:

```python
def __init__(self, tasks_dir, state_dir, followers_fn, alive_fn, ...)
def _live_followers(self):
    return [f for f in self.followers_fn() if _INST_RE.match(str(f)) and self.alive_fn(f)]
```

A follower appearing or vanishing is already an ordinary input. The fixedness
lives entirely in `scripts/install-worker-pool.sh N`, which writes N plists at
install time. So this is an **authority gap, not an architecture one**: the lead
can observe the fleet but cannot change it.

### Create / preempt / restore

Owner-specified factors, with their roles made explicit — they are not four
parallel tests:

```
unassigned room / group                 TRIGGER   demand exists
all cores busy                          TRIGGER   no seat available
no core holds relevant context          TRIGGER   a seat exists, wrong one
sufficient compute after preempting     GATE      can we afford it
```

The first three are reasons to *want* a core; the fourth is a precondition on
getting one, and it names preempting inactive cores as the way to satisfy
itself. **Preempt is a step inside create, not a sibling of it** — implemented as
parallel policies, a create and a preempt race over the same resource budget.

**Restore-after-preempt is load-bearing.** Preemption is acceptable only if it
is reversible, and reversible means the group→core binding **outlives the core**.
So the binding is a record the lead holds, with the core as its current
occupant; restore is not "start a core" but "re-seat this binding".

Open, and the one knob wanting a measurement rather than a rule: how long a core
must be idle to count as preemptable. Too short thrashes, too long starves.
`PoolMetrics` already emits per-channel latency and `head_of_line_incidents`, so
it can be fitted rather than picked.

## Decision 5 — the periodic sweep belongs to the LEAD, not to each follower

Today each follower registers its own `*/5` `/proactive-loop-pool pass`, whose
only job is, per that skill, *"the periodic sweep that catches assignments the
watcher missed."* It is a backstop; pickup is the watcher event.

Two things are wrong with siting the backstop in the follower:

- **It is O(N).** N followers each wake per period to answer "did I miss
  anything?" Nine idle followers is nine wakeups to answer "no" — and N is
  exactly what dynamic sizing varies.
- **The follower is the wrong party to ask.** It sees only its own assignments.
  The lead already knows *what it assigned and to whom*, so "this assignment was
  never claimed" is computable from state it holds, without waking anyone.

**So: an unclaimed-assignment timeout on the lead, and followers purely
event-driven.** One timer instead of N, no model calls (the lead is plain code),
and it composes with preemption — a preempted core's unclaimed work is the same
condition, not a special case.

Lead death is what would otherwise keep a follower-side timer honest, and #3314
already covers it: followers "degrade to leaderless claiming on lead loss". That
path stands without a periodic sweep behind it.

## Decision 6 — subagent vs new core: the discriminator is SHARED CONTEXT

Both add parallelism, so the design needs a rule for which one, or the two
policies compete for the same resource budget.

```
work SHARES the group's context   ->  subagent, inside the seated core
work is a NEW context group       ->  the create path (decision 4)
```

A subagent costs no seat, no binding, no pane, and no lead involvement — it runs
inside the core already holding that group's conversation, which is exactly why
it is the right tool for intra-group fan-out. Creating a core is the expensive
operation: a new plist, a new session, a new binding the lead must then own,
reassign on death, and restore after preemption.

**This is what makes decision 2 affordable.** The objection to binding a group to
a fixed core is that it collapses least-loaded fallback into a static table. That
objection only holds if task-level distribution has to supply the parallelism. It
does not: subagents supply it *within* a group, so the pool's job narrows to
placement *across* groups, and a static table is the correct structure for
placement.

So the create factors in decision 4 are reached only after this test. "All cores
are busy" is not by itself a reason to create — if the work belongs to a group
that is already seated, the answer is a subagent in that core, and creating one
would fragment a conversation across two sessions.

**Corollary for preemption:** a core running subagents is not idle, however its
own turn-taking looks. Whatever idle threshold decision 4 settles on has to count
subagent activity, or the pool will preempt exactly the cores doing the most work.

## Costs, stated rather than discovered later

1. **Per-group concurrency is 1, by definition.** One thread, one turn at a
   time. Parallelism is *across* groups. Intra-group parallelism is subagents,
   not more cores — which is what makes decision 2 affordable.
2. **Head-of-line inside a busy group.** Its tasks queue behind its own session
   while other cores idle. `head_of_line_incidents` already measures it, so this
   is observable rather than arguable.
3. **A bound core dying takes its groups down** until something re-seats them.
   Affinity degraded gracefully here; binding does not. The lead's dead-follower
   reclaim must reassign that core's **groups**, not just its in-flight tasks.
4. **The lead gains the ability to start and stop processes.** That is a
   meaningfully larger blast radius than assigning a file, and the reason
   decision 4 should land after #3314 rather than inside it.

## Sequencing

#3314 lands as-is. #3263 needs its live round trip either way. Decisions 2, 3
and 5 are a third change that comes after both.

The only thing needing a decision *now* is the one-line constraint in decision 3:
keep pane ownership outside the follower. Deciding it now costs nothing — but
implementing it is not free: as established above, it requires coordinated
changes to the reclaim parser, `_load`, the busy predicate, and group-key
encoding. What the early decision buys is that none of those changes must be
retrofitted under pressure later; every composition above stays available.
