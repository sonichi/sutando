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

- `pool_beat.py` — refresh liveness; worker writers watch the tmux CLI PID each second and check PID reuse before each refresh. Unreadable process metadata is nonfatal.
- `spawn_worker.py` — mint a worker: identity records, delivery folder, tmux session, watcher, detached worker beat; refuses before any side effect, rolls back on a launcher failure.
- `create_worker.py` — the one command that spawns and registers under the roster lock, so the roster cannot go stale.
- `rename_worker.py` — change a worker's base routing alias after creation: `python3 skills/worker-pool/scripts/rename_worker.py --worker <id-or-label> --label "<new alias>" [--workspace W]`. The id and its tmux session name stay.
- `apply_profile_label_overrides.py` — apply an s.space profile's complete worker display-label map from JSON stdin with `--workspace W --profile-mxid M --config-version N`.
- `worker_bootstrap.py` — a worker session's first-turn decision (worker vs core mode) from its env.
- `pool_roster.py` — owner bindings + compiled roster; `register_worker` is the locked read-merge-write. `set_worker_state` / `remove_worker` are its writers for a worker's lifecycle.
- `pool_manage.py` — stop, retire, re-enable or remove a worker (below).
- `pool_identity.py` — give a worker an s.space agent of its own, show it, rotate its token or remove it (below).
- `pool_provision.py` — the local half of creating that agent without a pasted token: claim secret, redeem, retire (below).
- `provision_e2e.py` — check that whole flow against one server, with a scan for leaked secrets (below).
- `worker_identity.py` — worker / session / incarnation records.
- `pool_router.py` — resolve one task to its recipients from the roster.
- `pool_route_handler.py` — the core watcher's task-event handler (`SUTANDO_TASK_EVENT_HANDLER`): declines unbound work, delivers bound work as sentinels.
- `pool_delivery.py` — a recipient's own folder: sentinels in, accept, release, done flags, and the owner's `hand_off_to_core`.
- `pool_ask.py` — ask another instance a question through the front door (below).
- `pool_sessions.py` — read-only: which worker tmux sessions a viewer could attach, and the exact-match argv for each. Adds no state and nothing in the pool calls it; it exists for the desktop app's terminal picker. It deliberately does not describe the core, whose socket resolution belongs to the app.

```bash
python3 skills/worker-pool/scripts/pool_sessions.py list --workspace "$WS" [--room '!id:ag2.space']
```

Suites live at `tests/skills/worker-pool/`.

## Managing workers: stop, retire, enable, remove

Killing a worker's tmux session does not stop it: the remedy timer's next sweep
(every 5 minutes) sees a dead session and resumes it on the same conversation.
Use `pool_manage.py`, which records the owner's decision where the remedy, the
sweep and the router read it. Run from the repo root; `--workspace` defaults to
the resolved workspace. Each verb prints one JSON line; exit 0 done, 1 failed,
2 refused (unknown or ambiguous name, `core`, the caller's own worker, a bound
worker, or a worker that still owes work).

```bash
python3 skills/worker-pool/scripts/pool_manage.py list [--json]
python3 skills/worker-pool/scripts/pool_manage.py stop   <worker id or label> [--force]
python3 skills/worker-pool/scripts/pool_manage.py retire <worker> [--unbind] [--hand-off]
python3 skills/worker-pool/scripts/pool_manage.py enable <worker> [--no-start]
python3 skills/worker-pool/scripts/pool_manage.py remove <worker> [--hand-off]
```

- `stop` writes the worker's pause (`pool_supervise.set_paused`) under the remedy
  lock, then kills its `-watcher` supervisor, `-input` watcher and seat sessions
  on the socket it last ran on, and closes its open run as `killed`. A paused
  worker is not restarted by a sweep, `--resume` or `--restart`. Routing does
  not change: bound-room and requested-worker tasks keep arriving in its inbox
  and wait for `enable`. A worker that owes a delivery is refused unless
  `--force`; the owed work stays its own and resumes after `enable`.
- `retire` fences the worker first: roster state `retired`, so the router sends
  work addressed to it to the core, and every delivery re-checks that state under
  the folder lock the hand-off takes, so a pass that read the roster earlier cannot
  commit a new task to it either; once `remove`d, the tombstone it leaves in the
  roster's `removed` list keeps that fence. Then it is taken down like `stop`, then owed work
  is handed off, then its bindings are dropped. Refused before any write while a
  room is bound to it (unless `--unbind`) and while it owes a delivery, pending or
  accepted (unless `--hand-off`). The hand-off (`pool_delivery.hand_off_to_core`)
  runs once its sessions are confirmed gone and repeats until a listing under the
  folder lock finds nothing left. It withdraws the sentinel, the `.pending` record
  and the attribution together, so a replay is declined to the core and the
  core's queue counts the task. A task whose result already landed is kept as the
  worker's reply, never run again. If tmux cannot confirm the sessions gone, the
  worker stays retired (routing closed) and paused and keeps its work: re-run
  `retire`, or `enable` it.
- `enable` sets a retired worker back to `live`, lifts the pause, and runs the
  owner restart (`pool_remedy --restart`), which resumes its conversation.
  With `--no-start` it only lifts them; the sweep's death ladder then restarts
  the dead session like any other, after its sustained ticks.
- `remove` drops a retired worker's roster row (same `--hand-off` rule), leaving
  its id in the roster's `removed` list so late deliveries stay fenced. Its
  records under `state/workers/<id>/` and its `deliveries/<id>/` folder stay as
  history.

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

s.space can set an optional `display_label` override for a worker. It appears
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
an s.space override restores the base label as the visible name.

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

## A worker's own agent

By default a worker answers through the core's agent. A worker can instead run as an agent of its own: the owner
creates that agent in s.space, and its access token becomes a connection row naming the worker
(`src/connections.py`, "a pool worker's own identity"). That row runs a lane of its own:
- It answers only what is addressed to that agent.
- It hands every task it queues to the worker (`requested_worker`).
- It posts nothing proactively.
- It never becomes primary.

The core owns that identity's lifecycle and its lane; the worker's session uses it. From the worker's seat, room
clients (`room_ops.py`, `notify.py`, the room-database adapters) act as the worker's own agent in rooms on its
server, through the one resolver (`connections.acting_identity`, see `docs/connections.md`, "In a pool worker's
session"). A task a core lane routed to the worker is still answered as the core's agent.

```bash
python3 skills/worker-pool/scripts/pool_identity.py add <worker> --repo . --logs-dir "$(bash scripts/sutando-config.sh workspace)/logs"
python3 skills/worker-pool/scripts/pool_identity.py show <worker>
python3 skills/worker-pool/scripts/pool_identity.py rotate <worker> --repo . --logs-dir <logs dir>
python3 skills/worker-pool/scripts/pool_identity.py remove <worker>
```

`add` asks for the token at a hidden prompt (`-` reads it from stdin), then shows what the token's gateway says and
asks before saving. It refuses an agent the gateway does not name (`worker_identity_unverified`) and one that another
row already runs as (`worker_identity_shared`). It also refuses an agent whose owner is not the owner a core
connection already serves (`worker_owner_mismatch`), so a worker never runs as another person's agent. The token goes into the secret vault. It never goes into a file and is
never printed. `rotate` accepts a new token only for the same agent. A worker's lane is launched naming its worker. If
the launch and the record disagree, or neither can say, the lane refuses to start rather than run as the core's.

`pool_manage.py retire` also removes the worker's agent identity: its lane, its row and its vault secret. The agent
itself stays on its server until the owner deletes it there.

### Creating the agent without a pasted token

The core can create a worker's agent itself, so nobody pastes a token. The server side is the `agent.create`
account Action, which needs the owner's approval and answers with a claim, never a token. The token never
passes through a model: only these scripts and the secret vault hold it. Use this path only when the owner asked
for the worker to have its own agent.

1. `pool_identity.py provision-begin <worker>` makes a one-time secret, keeps it only in the secret vault and
   prints one JSON line: `claim_key_sha256` (the secret's SHA-256) and a suggested `name`. The server names the
   agent `@<owner>-<core>-<name>.agent`, so the name is the worker's role alone.
2. `pool_identity.py provision-wait <worker> --operation-id <id> --name <name>` does the rest. It calls
   `agent.create` as the core's own agent, asks again every 10 seconds with the same operation id until the
   owner has approved it, then redeems the claim at once and saves the answered token as the worker's
   connection, the same row and vault key `add` writes. It prints the agent id and a status and starts the lane.
   Tell the owner in one line that an approval is waiting for them.

```bash
python3 skills/worker-pool/scripts/pool_identity.py provision-begin <worker> [--role ROLE] [--lane INSTANCE]
python3 skills/worker-pool/scripts/pool_identity.py provision-wait <worker> --operation-id <new id> --name <name> \
    [--display-name TEXT] [--role TEXT] [--timeout SECONDS]
python3 skills/worker-pool/scripts/pool_identity.py provision-finish <worker> --claim-id <claim id> --agent-id <agent id>
python3 skills/worker-pool/scripts/pool_identity.py provision-abort <worker>
python3 skills/worker-pool/scripts/pool_identity.py retire-remote <worker> [--local-anyway]
```

- Nothing tells an agent that its held call was approved, and a claim lives five minutes from the moment the
  agent exists. That is why `provision-wait` makes the call and the redeem itself. Let it make the call: the
  same operation id with other arguments is refused (`operation_claimed`). If you already called `agent.create`
  and hold its `agent_id` and `claim_id`, use `provision-finish` within those five minutes instead.
- A wait that times out (15 minutes unless `--timeout`) keeps the secret, because the request may still be
  approved: run `provision-wait` again with the same operation id and arguments, or `provision-abort`. A
  refusal forgets the secret and names the reason. `approval_required` means the server cannot ask the owner
  yet: stop, and a person adds the agent in the app. A core may create five agents an hour.
- The redeemed token is saved only when the redeem answer and the gateway both name the agent `agent.create`
  returned. The secret is deleted when the redeem ends, whether it worked, failed or was interrupted. A failure
  names what failed (the HTTP status and the server's reason) and never echoes a secret.
- If the redeem worked but the token could not be confirmed or saved, the message names the agent: it now has a
  token nobody holds, so retire it and provision again. A dead claim (`claim_expired`, `claim_burned`) needs the
  same.
- The claim is bound to the core that created it and to the secret. Without `--lane` the core's lane is the app's
  signed-in one, else the primary.
- To retire, call the account Action `agent.retire` with the `agent_id` and an `operation_id` first (it needs the
  owner's approval too; on `soft_delete_incomplete` call it again), then `retire-remote`. It removes the lane,
  the row and the vault secret only once the gateway rejects the stored token, and exits 3 with nothing removed
  while the token still works. `--local-anyway` removes them regardless and exits 1: tell the owner the agent is
  still live on its server.

`provision_e2e.py --base-url https://<gateway> --worker <worker> [--room ROOM]` runs all of this against one
server and prints a pass/fail table: the approval gate, the agent's provenance and name, the redeem and each way
it must be refused, the worker answering a mention, the retirement, and a scan of the workspace, the session
transcripts, the shell history and the core's and worker's tmux scrollback for the token, the claim secrets and
the core's own credential (expected only in its own configured env file). The scan compares in the process and prints places
and counts only, and first has to find a control value it plants in the workspace's own logs. Its `--help` says
what a run creates on the server.

## Binding a room to an existing worker

`pool_roster.py:bind_room(workspace, room, target)` pins one room to one
already-registered worker (by id or unique label) — the picker's "pin" action
and `pool_roster.py bind` both go through it. **It does not call
`publish_task_event_handler()`.** `register_worker()` (and therefore
`create_worker.py`) does, on every call, because a registration that leaves
the core watcher without its route handler is a pool the launcher cannot
recognize (see its docstring). `bind_room()` only writes the binding and
recompiles the roster/advertisement — nothing declares the handler.

That handler — `state/task-event-handler.json`, pointing at
`pool_route_handler.py` — is what makes the core watcher hand a bound room's
tasks to its worker instead of answering them itself. If it was never
published (a fresh install whose only registrations ever went through
`bind_room()`, or one where the file was lost), every room pinned since then
silently falls through to the core: the room still gets an answer, just from
the wrong instance, and nothing errors. Check with (from the repo root, so
the relative `sys.path` inserts resolve):

```bash
WS="$(bash scripts/sutando-config.sh workspace)"
python3 -c "
import sys; sys.path.insert(0, 'src')
from util_paths import task_event_handler_config_path
print(task_event_handler_config_path('$WS/state').exists())
"
```

`False` (or the equivalent: `bindings.json` names a worker but its
`deliveries/<worker id>/` never gets a sentinel for that room's traffic) means
routing has never actually reached the worker. Stopgap, safe to run any time —
it is the exact call `register_worker()` makes, just without a registration
attached (also from the repo root):

```bash
WS="$(bash scripts/sutando-config.sh workspace)"
python3 -c "
import sys; sys.path.insert(0, 'skills/worker-pool/scripts')
from pool_roster import publish_task_event_handler
print(publish_task_event_handler('$WS'))
"
```

The core's watcher fswatches that path, so this takes effect immediately —
no restart. It is a one-time fix for this host's *current* state, not a code
fix: `bind_room()` itself still won't publish on the *next* fresh pin until
it is taught to (tracked in #4580). Re-run the stopgap if
`state/task-event-handler.json` is ever lost, or if a room is pinned via
`bind_room()` on an install that has never registered a worker through
`register_worker()`/`create_worker()`.

**Finding your own name.** `pool_ask.py --who` already marks your own row:
`whoami()` compares `$SUTANDO_INSTANCE_ID` against every row (it still lists
every worker, not just yours), text output appends `  (you)` to the matching
one, and `--json` sets `"me": true` on it:

```bash
python3 skills/worker-pool/scripts/pool_ask.py --workspace "$WS" --who
```

Add `--json` for `"me": true`. Built from the roster
(`pool_roster.py:load_roster`), not the advertisement file —
`state/pool-advertisement.json`'s `profile_workers` map carries only `label`
and (when known) `runtime`, never `display_label` or a self-marker.

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
  alias separately when an s.space display override differs.
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
