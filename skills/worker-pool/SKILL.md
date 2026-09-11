---
name: worker-pool
description: "Worker-pool modules that the core does not need: the owner-authored bindings + compiled roster the router reads, and a worker's durable identity records (worker / session / incarnation). Optional — the core boots and delivers with this skill absent."
user-invocable: false
---

# Worker pool

The pool-specific half of the multi-worker design (`docs/worker-pool-design.md`,
`docs/core-pool-standing-sessions.md`). Everything here is only meaningful when
a pool exists, so it lives in a skill: a single-core install (N=0 workers) never
loads it and must keep working without it.

## What lives here

| Script | Purpose |
| --- | --- |
| `scripts/pool_roster.py` | Bindings the owner writes; a roster the core compiles; the router only reads. |
| `scripts/worker_identity.py` | A worker's durable identity: which worker, which conversation, which run. |
| `scripts/pool_delivery.py` | The `deliveries/<recipient>/` sentinel grammar: accept, hold, release. |

Tests are the skill's own: `tests/*.test.py`, discovered by CI's
`find tests skills -name '*.test.py'` and measured by the same coverage gate.

## What stays in src (the boundary)

The test is *who reads it with zero workers configured*: the core reads a
roster or a worker identity never, and — measured on `main`, not assumed —
reads delivery records never either.

```
$ git grep -c -E 'deliveries|pool_delivery' origin/main -- \
      src/check-pending-tasks.sh src/watch-tasks-stream.sh
origin/main:src/watch-tasks-stream.sh:1

$ git grep -n 'import pool_delivery|from pool_delivery' origin/main -- src/
(no output)
```

The one hit is `watch-tasks-stream.sh:86`, a COMMENT about workspace resolution
-- not a read. No file in `src/` imports `pool_delivery`, which is the question
that decides the boundary; the word count never was.

An earlier draft of this section kept it in the core on the grounds that the task hook and watcher read it on every task — that describes
the caller #4167 would have added, and #4167 was closed before it landed.

So all three pool modules move together, and the boundary rule is unchanged — it
is the same rule, applied to a fact that moved.

## How the core reaches this skill

Nothing in `src/` imports these two modules — verified by
`git grep 'pool_roster\|worker_identity' -- src scripts`, which returns no hit
outside this skill. So there is no seam to maintain yet, and none was invented.

When a core-side caller does arrive, it must not hardcode the skill path: inject
it at the adapter edge (a `manifest.json` `config` entry per `skills/MANIFEST.md`,
or a path passed in by the caller). A `src/` module that *genuinely* cannot run
without one of these files is core by definition and belongs back in `src/`.

These scripts reach the core the other way — `parents[3] / "src"` for
`workspace_default.resolve_workspace`, the same bootstrap every other skill
script uses.

## More is coming

This is part A of an owner-directed restructure. The remaining pool work still
lands in `src/` today and must re-home here as each lands:

#4108 · #4110 · #4115 · #4119 · #4120 · #4121 · #4162 · #4175 · #4176

#4176 edits `src/pool_roster.py` directly and must be rebased onto this move.
