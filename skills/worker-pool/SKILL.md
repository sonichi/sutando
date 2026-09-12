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
