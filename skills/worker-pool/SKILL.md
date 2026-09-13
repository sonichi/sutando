---
name: worker-pool
description: "Worker-pool stage-1 modules. Placeholder: this move relocates the scripts; the skill's documentation is not written yet."
user-invocable: false
---

# worker-pool

**Placeholder.** `CONTRIBUTING.md` requires a `SKILL.md` beside `scripts/`; this file
satisfies that and nothing more.

Three stage-1 modules for the worker pool live in `scripts/`. They have no
production caller yet — their suites are the only thing that imports them:

```
$ grep -rnE 'pool_roster|worker_identity|pool_delivery' src/ scripts/
(no output)

$ grep -rlE 'pool_roster|worker_identity|pool_delivery' tests/
tests/skills/worker-pool/pool-bindings-roster.test.py
tests/skills/worker-pool/pool-delivery.test.py
tests/skills/worker-pool/worker-identity-records.test.py
```

Their suites are at `tests/skills/worker-pool/`, under the mandatory root every
runner already globs.

The design document these modules implement is **not on `main`** — it lands via
sonichi/sutando#4041. `pool_delivery.py`'s header cites `docs/worker-pool-design.md`
by path; that reference is inherited from before this move and is dead until #4041
merges. Until then the modules' own headers and their three suites are the
authoritative description.

Describing what each module owns is deliberately left to the PR that wires the pool
into the core, where the descriptions can be checked against a caller.

## Modules (`scripts/`)

- `spawn_worker.py` — mint a worker: identity records, delivery folder, tmux session, watcher; refuses before any side effect, rolls back on a launcher failure.
- `create_worker.py` — the one command that spawns and registers under the roster lock, so the roster cannot go stale.
- `worker_bootstrap.py` — a worker session's first-turn decision (worker vs core mode) from its env.
- `pool_roster.py` — owner bindings + compiled roster; `register_worker` is the locked read-merge-write.
- `worker_identity.py` — worker / session / incarnation records.
- `pool_router.py` — resolve one task to its recipients from the roster.
- `pool_route_handler.py` — the core watcher's task-event handler (`SUTANDO_TASK_EVENT_HANDLER`): declines unbound work, delivers bound work as sentinels.
- `pool_delivery.py` — a recipient's own folder: sentinels in, accept, release, done flags.

Suites live at `tests/skills/worker-pool/`.
