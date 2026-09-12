---
name: worker-pool
description: "Worker-pool stage-1 modules: bindings/roster compilation, worker identity records, and recipient-side delivery-sentinel reading. Library modules with no production caller yet — the pool is not wired into the core at this head."
user-invocable: false
---

# worker-pool

Three library modules for the worker pool described in `docs/worker-pool-design.md`.

**Nothing in the repository calls them at this head.** Verified, not assumed:

```
$ grep -rn 'pool_roster|worker_identity|pool_delivery' src/ scripts/
(no output)
```

They are staged ahead of the core wiring, so the core boots identically with this
directory absent. Their own suites are the only callers.

## What each module owns

**`scripts/pool_roster.py`** — two files with different authors, which is the point.
`state/bindings.json` is owner-authored, one durable addressee per line of work;
`state/roster.json` is compiled by the core from workers, bindings and states. The
router reads the roster and nothing else. Compilation lives here rather than in the
router so routing stays testable by replay: same roster, same task, same decision.

**`scripts/worker_identity.py`** — three questions with three different answers,
and collapsing any two loses what the others cannot recover.

```
worker_id           which worker is this?     kept for the worker's life
runtime_session_id  which conversation?       carried over on resume
incarnation_id      which RUN of this worker? new each execution instance
```

**`scripts/pool_delivery.py`** — the delivery-side half, stage 1 of the design. A
recipient reads its *own* folder. Work arrives as **sentinels**, not copies: the
payload stays immutable at `tasks/<task-id>.txt`, and the existence of
`deliveries/<me>/<task-id>.txt` *is* the assignment. Acceptance substitutes the
suffix rather than appending. It reads assignments in; it does not send results back.

## Paths

Each module re-adds the repo's `src/` to `sys.path` for the core's
`workspace_default`; the repo root is `parents[3]` from `scripts/`.

## Tests

The suites are at `tests/skills/worker-pool/`, not inside this directory.

Every runner globs `find tests -name '*.test.py'` — recursive over `tests/`, and it
does **not** descend into `skills/`. A suite placed at `skills/worker-pool/tests/`
would stop running with CI still green. Under the mandatory root, no discovery
change is needed in `package.json`, `.github/workflows/ci.yml` or
`scripts/coverage-gate.sh`. Coverage still reaches the scripts: `.coveragerc`
already lists `skills` as a source root.
