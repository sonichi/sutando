---
name: worker-pool
description: "Create, route, supervise, and communicate with Sutando workers."
user-invocable: false
---

# worker-pool

Worker-pool commands live in `scripts/`; their suites live in
`tests/skills/worker-pool/`. The durable identity, routing, and delivery
contracts are in [`docs/worker-pool-design.md`](../../docs/worker-pool-design.md).

## Modules (`scripts/`)

- `spawn_worker.py` — mint a worker: identity records, delivery folder, tmux session, watcher; refuses before any side effect, rolls back on a launcher failure.
- `create_worker.py` — the one command that spawns and registers under the roster lock, so the roster cannot go stale.
- `rename_worker.py` — change a worker's base routing alias after creation: `python3 skills/worker-pool/scripts/rename_worker.py --worker <id-or-label> --label "<new alias>" [--workspace W]`. The id and its tmux session name stay.
- `apply_profile_label_overrides.py` — apply an AG2 Space profile's complete worker display-label map from JSON stdin with `--workspace W --profile-mxid M --config-version N`.
- `worker_bootstrap.py` — a worker session's first-turn decision (worker vs core mode) from its env.
- `pool_roster.py` — owner bindings + compiled roster; `register_worker` is the locked read-merge-write.
- `worker_identity.py` — worker / session / incarnation records.
- `pool_router.py` — resolve one task to its recipients from the roster.
- `pool_route_handler.py` — the core watcher's task-event handler (`SUTANDO_TASK_EVENT_HANDLER`): declines unbound work, delivers bound work as sentinels.
- `pool_delivery.py` — a recipient's own folder: sentinels in, accept, release, done flags.
- `pool_ask.py` — ask another instance a question through the front door (below).
- `pool_sessions.py` — read-only: which worker tmux sessions a viewer could attach, and the exact-match argv for each. Adds no state and nothing in the pool calls it; it exists for the desktop app's terminal picker. It deliberately does not describe the core, whose socket resolution belongs to the app.

```bash
python3 skills/worker-pool/scripts/pool_sessions.py list --workspace "$WS" [--room '!id:ag2.space']
```

Suites live at `tests/skills/worker-pool/`.

## Codex CLI workers

`python3 skills/worker-pool/scripts/create_worker.py --runtime codex --folder
<dir> --label <name>` creates a Codex worker with its own tmux session and
notifier. The core watcher needs the pool route handler before `--room` can
bind a room. An unbound worker can receive tasks addressed to its ID.

Codex assigns new conversation IDs itself. Until its assigned ID is captured,
the worker's `runtime_session_id` is `null`, `--resume` refuses, and recovery
starts a fresh Codex conversation with the same worker ID and inbox. The
notifier resolves delivery sentinels to payloads in shared `tasks/`, writes
results to shared `results/`, and records the worker's done flag.
If the worker dies mid-turn, recovery re-delivers the task in that fresh
conversation without memory of any partial work from the previous turn.

## Worker names and identities

The 32-hex `worker_id` is stable and is the roster key, delivery recipient, and
room binding target. The roster's base `label` is a routing alias.
`rename_worker` changes it without changing the ID.

AG2 Space can set an optional `display_label` override for a worker. It appears
in the picker, session list, pool advertisement, and human status. An exact
full ID or `core` always selects that recipient. A unique base or display label
can address a worker in `pool_ask --to`, room binding, and task requests; a
human name shared by workers is refused. New broker display labels equal to
`core`, any existing worker ID, or shaped like a full 32-hex ID are refused.
Registration refuses a new ID already used as another worker's name. Human
status uses `name (full worker_id)`, for example
`kc-reviewer-ryan (274cb60d473744dba54040a9de119877)`. The `pool_ask --who`
JSON keeps `label` as the base routing alias and adds `display_label` as the
effective name; text adds `alias=<base label>` when the names differ. Removing
an AG2 Space override restores the base label as the visible name.

`apply_profile_label_overrides.py` reads the complete override map, keyed by
full worker ID, under the roster lock. It tracks the broker snapshot in
`worker_label_config_version`, separately from the roster's general
`config_version`, and scopes it to `worker_label_profile_mxid`. Re-enrollment
to a new profile accepts that profile's lower version. Retired IDs are
ignored; a pending unknown ID prevents the label version from advancing so a
later poll can apply it after registration. Other roster compiles preserve
these fields. A broker label edit can persist without a version bump, so a
repeat of the current version follows the owner's complete map and repairs
local label drift.

## Talking to the other instances (core ↔ worker)

You are one instance of a pool: the **core** (the canonical session, owning `tasks/`)
and zero or more **workers**, each with its own tmux session, watcher and inbox
(`deliveries/<worker id>/`). There is deliberately no back channel between them —
what there is, is the task file. `pool_ask` uses it, so an ask is an ordinary task
the owner can see, and a reply is an ordinary result.

**A worker's queue is its own inbox and nothing else.** `tasks/` holds every
instance's payloads, the core's and every other worker's in flight; a task is yours
only while its sentinel sits in `deliveries/<your id>/`. Never list `tasks/` to find
work, and never answer a task file you found there: the result would be posted as a
reply in a room bound to someone else. The watcher's `QUEUE: n pending after this`
counts your inbox, and that count is the only queue you have.

```
python3 skills/worker-pool/scripts/pool_ask.py --workspace "$WS" --who
python3 skills/worker-pool/scripts/pool_ask.py --workspace "$WS" --to <label|id|core> --ask "..." [--wait 300]
```

- `--who` lists `core` and each worker as `name (full worker_id)`, with bound
  rooms and session liveness; `(you)` marks the caller. It shows the base routing
  alias separately when an AG2 Space display override differs.
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
- **Relaying someone else's question** — a peer's, a guest's — is `--relayed-from <who>
  --tier <team|other|guest>`: the task keeps *their* tier and carries `relayed_from:` for the
  receiver to key on. `owner` is never a tier an ask can claim, and non-owner content must
  never be relayed as your own: the tier must say where the question came from, not who ran
  the script.
- Refused, never guessed: an unknown name, a name shared by recipients, and asking yourself.

Not yet: the reply is read from `results/` by the asker, not delivered into the
asker's inbox (`reply_to_instance` is recorded for that later leg), and a worker
learns this section by reading it — surfacing it in `/startup --worker` is one line
in the startup skill, outside this one.
