---
name: worker-pool
description: "Worker-pool internals: roster bindings, worker identity records, and result delivery for a multi-worker core. Library modules invoked by the core and by the pool's own tooling — not a user-facing command."
user-invocable: false
---

# worker-pool

Three library modules the core loads only when workers are configured. Nothing here
is a slash command, and the core boots without this directory present.

| script | what it owns |
|---|---|
| `scripts/pool_roster.py` | the bindings file: which worker answers for which seat, and refusal of a mis-shaped one |
| `scripts/worker_identity.py` | durable per-worker identity records — id allocation, reuse refusal, workspace resolution |
| `scripts/pool_delivery.py` | delivery of a worker's result back to the task that asked for it, plus its CLI |

They import the core's `workspace_default` from `src/`, so each re-adds the repo's
`src/` to `sys.path`; the repo root is `parents[3]` from this directory's `scripts/`.

## Tests

The suites live at `tests/skills/worker-pool/`, not inside this directory.

That is deliberate and matches all other skills: every runner globs `find tests
-name '*.test.py'`, which is recursive over `tests/` and does **not** descend into
`skills/`. A suite placed at `skills/worker-pool/tests/` would stop running with CI
still green. Keeping them under the mandatory root needs no discovery change in
`package.json`, `.github/workflows/ci.yml` or `scripts/coverage-gate.sh`.

Coverage still measures the scripts: `.coveragerc` already lists `skills` as a
source root.

## Without the pool

Zero workers configured means the core never imports any of these — verified on
`main` rather than asserted:

```
git grep -nE 'import pool_delivery|from pool_delivery' origin/main -- src/
(no output)
```
