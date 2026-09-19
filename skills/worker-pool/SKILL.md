---
name: worker-pool
description: "Worker-pool stage-1 modules. Placeholder: this move relocates the scripts; the skill's documentation is not written yet."
user-invocable: false
---

# worker-pool

**Placeholder.** `CONTRIBUTING.md` requires a `SKILL.md` beside `scripts/`; this file
satisfies that and nothing more.

Stage-1 modules for the worker pool live in `scripts/`. Only
`resolve_inbox_entry.py` has a production caller — the core watcher execs its
`resolve-inbox-entry` wrapper as `$SUTANDO_INBOX_RESOLVER`; the rest are imported
only by their suites:

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
merges. Until then the modules' own headers and their suites are the
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
- `pool_ask.py` — ask another instance a question through the front door (below).

Suites live at `tests/skills/worker-pool/`.

## Talking to the other instances (core ↔ worker)

You are one instance of a pool: the **core** (the canonical session, owning `tasks/`)
and zero or more **workers**, each with its own tmux session, watcher and inbox
(`deliveries/<worker id>/`). There is deliberately no back channel between them —
what there is, is the task file. `pool_ask` uses it, so an ask is an ordinary task
the owner can see, and a reply is an ordinary result.

```
python3 skills/worker-pool/scripts/pool_ask.py --workspace "$WS" --who
python3 skills/worker-pool/scripts/pool_ask.py --workspace "$WS" --to <label|id|core> --ask "..." [--wait 300]
```

- `--who` lists every recipient — `core` and each worker by label — with its bound
  rooms and whether the supervisor sees its session alive; `(you)` marks the caller.
- `--to X --ask "..."` writes `tasks/<task id>.txt` with `source: pool-ask`,
  `requested_worker: <id>` (never for the core, which is the default recipient) and
  `reply_to_instance: <asker>`, then routes it. A worker finds it in its inbox like
  any other delivery; the core's watcher takes an ask addressed to the core.
- **To answer an ask**, write `results/<task id>.txt` whose first line is `[no-send]`
  — the asker reads the file (with `--wait`, as soon as it lands) and nothing is
  posted to any room. The ask's body says so, so an answerer needs no other briefing.
- An ask carries `access_tier: team` + `collaborator: true` + `priority: low`: the asker is
  another instance, not the owner, so a standing ask never reads as an owner waiting to
  `scripts/cron-gate.sh` or the shepherds (measured: an owner-tier ask deferred `sync-workspace`).
- Refused, never guessed: an unknown name, a label two workers share, and asking yourself.

Not yet: the reply is read from `results/` by the asker, not delivered into the
asker's inbox (`reply_to_instance` is recorded for that later leg), and a worker
learns this section by reading it — surfacing it in `/startup --worker` is one line
in the startup skill, outside this one.
