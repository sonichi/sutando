---
name: worker-pool
description: "Worker-pool modules that the core does not need: the owner-authored bindings + compiled roster the router reads, the router pass and the task-event handler that runs it, a worker's durable identity records (worker / session / incarnation), the spawner that creates a worker, and the per-instance watcher gate its session boots through. Optional — the core boots and delivers with this skill absent."
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
| `scripts/pool_router.py` | The router pass: resolve one admitted task against the roster, write deliveries. |
| `scripts/pool_route_handler.py` | The router, as the core watcher's task-event handler. |
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
Its `--held` question and its `payload` subcommand are core for the same reason:
the watcher resolves a sentinel to its payload on every delivery-inbox event.

So does the watcher's side of the **task-event handler protocol** in
`src/watch-tasks-stream.sh` — the probe/real-run exit codes, the `UNSETTLED`
rc-5 branch that keeps a claim, and the `--retry-pass` it runs on a decline.
Those are numbers and a verb, not a file: the core runs whatever
`$SUTANDO_TASK_EVENT_HANDLER` names, and names nothing itself.

So do the launcher's worker-mode seams (`src/agent/*/start-cli.sh`,
`src/agent/codex/cli/task-notifier.sh`, `src/watch-tasks-stream.sh`): they are
the *core's own* `--runtime` selector and env allowlist, each gated on a variable
the core never sets. They forward what the spawner hands them and name no file
here — pinned by `tests/start-cli-worker-env-forwarded.test.sh`.

The line is *who reads it with zero workers configured*: the core reads delivery
records always, and reads a roster, a worker identity or a spawner never.

## How the core reaches this skill

Nothing in `src/` imports these modules — verified by
`git grep 'pool_roster\|pool_router\|pool_route_handler\|worker_identity\|spawn_worker\|worker_bootstrap' -- src scripts`,
which returns no hit outside this skill.

There are exactly **two seams**, and both run the other way — an env var the
core forwards and reads, never a path it holds.

`$SUTANDO_WORKER_BOOTSTRAP` names this skill's watcher gate for the core's
`/startup --worker` (in `skills/startup/SKILL.md`). `scripts/spawn_worker.py`
sets it in the worker session's env and `src/agent/claude/cli/start-cli.sh`
forwards it without knowing what it points at. Unset — a core install with this
skill absent — the startup skill treats it as `unknown` and starts nothing.

`$SUTANDO_TASK_EVENT_HANDLER` names `scripts/pool_route_handler.py`. The pool's
install sets it; `start-cli.sh` forwards it verbatim and never locates a handler
by filename (`tests/start-cli-task-event-handler-env.test.sh`). Three core
callers run it: the watcher's probe and real run, the watcher's `--retry-pass`
on a declined event, and the Stop hook's `--parked` question
(`src/check-pending-tasks.sh`). Unset, the watcher dispatches to the core as it
always did and the hook reports the task — nothing could have parked it, because
no router ran. Named but unable to answer, `--parked` exits 2 and the hook
reports nothing and says so: unknown is not a negative.

Any further core-side caller must inject the path the same way (that env var, or
a `manifest.json` `config` entry per `skills/MANIFEST.md`), never hardcode it. A
`src/` module that *genuinely* cannot run without one of these files is core by
definition and belongs back in `src/`.

These scripts reach the core the other way — `parents[3] / "src"` for
`workspace_default.resolve_workspace`, the same bootstrap every other skill
script uses.

## More is coming

This is part A of an owner-directed restructure. #4108 (the spawn launcher) and
#4110 (the router pass) are re-homed here; they land in that order. The
remaining pool work still targets `src/` today and must re-home as each lands:

#4115 · #4119 · #4120 · #4121 · #4162 · #4175 · #4176

#4176 edits `src/pool_roster.py` directly and must be rebased onto this move.
