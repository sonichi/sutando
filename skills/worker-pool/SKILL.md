---
name: worker-pool
description: "Worker-pool modules that the core does not need: the owner-authored bindings + compiled roster the router reads, a worker's durable identity records (worker / session / incarnation), the spawner that creates a worker, and the per-instance watcher gate its session boots through. Optional — the core boots and delivers with this skill absent."
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
| `scripts/spawn_worker.py` | Create a worker: an identity, a delivery folder, a tmux session, a watcher. |
| `scripts/worker_bootstrap.py` | Does THIS instance still need its watcher? The gate `/startup --worker` runs. |

Tests are the skill's own: `tests/*.test.py`, discovered by CI's
`find tests skills -name '*.test.py'` and measured by the same coverage gate.

## What stays in src (the boundary)

`src/pool_delivery.py` stays in the core. Despite the `pool_` name it is the
core's own **delivery-record grammar** — the sentinel/accept rules in
`deliveries/<recipient>/` that the core's task hook and watcher read on every
task, pool or no pool. Moving it would break a host with no skill installed.

So do the launcher's worker-mode seams (`src/agent/*/start-cli.sh`,
`src/agent/codex/cli/task-notifier.sh`, `src/watch-tasks-stream.sh`): they are
the *core's own* `--runtime` selector and env allowlist, each gated on a variable
the core never sets. They forward what the spawner hands them and name no file
here — pinned by `tests/start-cli-worker-env-forwarded.test.sh`.

The line is *who reads it with zero workers configured*: the core reads delivery
records always, and reads a roster, a worker identity or a spawner never.

## How the core reaches this skill

Nothing in `src/` imports these modules — verified by
`git grep 'pool_roster\|worker_identity\|spawn_worker\|worker_bootstrap' -- src scripts`,
which returns no hit outside this skill.

There is exactly **one seam**, and it runs the other way — the core's
`/startup --worker` (in `skills/startup/SKILL.md`) needs this skill's watcher
gate. It runs `$SUTANDO_WORKER_BOOTSTRAP`, which `scripts/spawn_worker.py` sets
in the worker session's env and `src/agent/claude/cli/start-cli.sh` forwards
without knowing what it points at. Unset — a core install with this skill absent
— the startup skill treats it as `unknown` and starts nothing.

Any further core-side caller must inject the path the same way (that env var, or
a `manifest.json` `config` entry per `skills/MANIFEST.md`), never hardcode it. A
`src/` module that *genuinely* cannot run without one of these files is core by
definition and belongs back in `src/`.

These scripts reach the core the other way — `parents[3] / "src"` for
`workspace_default.resolve_workspace`, the same bootstrap every other skill
script uses.

## More is coming

This is part A of an owner-directed restructure. #4108 (the spawn launcher) is
re-homed here and lands next. The remaining pool work still targets `src/` today
and must re-home as each lands:

#4110 · #4115 · #4119 · #4120 · #4121 · #4162 · #4175 · #4176

#4176 edits `src/pool_roster.py` directly and must be rebased onto this move.
