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

Tests are the skill's own: `tests/*.test.py`, discovered by CI's
`find tests skills -name '*.test.py'` and measured by the same coverage gate.

## What stays in src (the boundary)

`src/pool_delivery.py` stays in the core. Despite the `pool_` name it is the
core's own **delivery-record grammar** — the sentinel/accept rules in
`deliveries/<recipient>/` that the core's task hook and watcher read on every
task, pool or no pool. Moving it would break a host with no skill installed.

The line is *who reads it with zero workers configured*: the core reads delivery
records always, and reads a roster or a worker identity never.

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
