# Remote relay protocol

`src/remote-relay-bridge.py` lets a remote HTTP server dispatch tasks to a local
Sutando instance and collect the results — turning Sutando into a remotely
drivable worker without exposing the host (no open port, no tunnel). The bridge
is the **client**; you (or a service) provide the **relay server** that speaks
the contract below. Any server implementing these four endpoints can drive
Sutando — the protocol is provider-neutral.

The bridge is an optional channel, structurally identical to the
discord/telegram/slack bridges: it starts from `src/startup.sh` only when a
channel `.env` supplies a token, and is silent otherwise.

## Configuration

The bridge reads these from the environment (typically sourced from
`channels/<provider>/.env`):

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `REMOTE_TASK_URL` | yes | — | Relay base URL (e.g. `https://relay.example.com`). |
| `REMOTE_TASK_TOKEN` | yes | — | Bearer token sent on every request. |
| `REMOTE_TASK_PROVIDER` | no | `remote` | Label written as a task's `source:` when the task omits one. |
| `REMOTE_TASK_POLL_WAIT` | no | `25` | Long-poll seconds requested per `/v1/tasks` call. |
| `REMOTE_TASK_TIER` | no | `team` | Local access tier stamped on every inbound task (see Security). |

**Use the split form** (`REMOTE_TASK_URL` + `REMOTE_TASK_TOKEN`) — it's the recommended way to configure the bridge.

> **Legacy / bootstrap shortcut:** the bridge also accepts a *combined* token of the form `REMOTE_TASK_TOKEN="https://relay.example.com|<secret>"` (URL and secret joined by `|`), which it splits at startup. This exists only so a one-shot onboarding string can carry both halves. If you use it, **quote it in `.env`** — an unquoted `|` is a shell pipe when the file is sourced. Prefer the split form for anything persistent.

## Transport

- All requests carry `Authorization: Bearer <REMOTE_TASK_TOKEN>`.
- Request/response bodies are JSON.
- The protocol is versioned under the `/v1` path prefix.

## Endpoints

### `GET /v1/tasks?wait=<sec>`

Long-poll for pending tasks. The server should hold the connection up to `<sec>`
seconds and return as soon as work is available.

```
200 OK
{ "tasks": [ { "id": "task-123", "task": "summarize this", "source": "...", ... }, ... ] }
```

Return `{"tasks": []}` on long-poll timeout. The client uses an HTTP timeout of
`wait + 10s`, so the server must respond within `wait` seconds.

A task object **must** carry a unique `"id"`. Any additional string fields
(`task`, `source`, `channel_id`, `user_id`, `priority`, …) are written verbatim
into the local task file the core consumes.

### `POST /v1/tasks/<id>/ack`

Claim/acknowledge a task so the server stops redelivering it.

```
body: { "id": "task-123" }
```

The client acks each task as it is accepted. A server with at-least-once
delivery should treat ack as "stop redelivering"; the client is idempotent and
will not re-queue a task it already claimed or archived.

### `POST /v1/results`

Return a task's result.

```
body: { "id": "task-123", "body": "<result text>" }
```

### `POST /v1/heartbeat`

Periodic liveness + capability ping.

```
body: {
  "client": "sutando-relay-client",
  "protocol_version": 1,
  "provider": "<REMOTE_TASK_PROVIDER>",
  "tier": "<REMOTE_TASK_TIER>",
  "inflight": <int>,            // tasks currently claimed but not yet resulted
  "capabilities": ["task-ack", "heartbeat", "result-skip-markers"]
}
```

## Delivery + idempotency

- Delivery is assumed **at-least-once**. The client persists its in-flight set
  and restores it across restarts, so a task redelivered after a crash is not
  run twice.
- A task whose `id` is already queued, claimed, or archived locally is dropped
  (idempotent write).

## Security

- Inbound tasks are **not trusted to set their own access tier.** The bridge
  stamps every task with the local `REMOTE_TASK_TIER` (default `team`) as the
  last `access_tier:` line, so a task body cannot forge a higher tier. Set
  `REMOTE_TASK_TIER=owner` in the channel `.env` only for a relay you fully
  control.
- The token is a per-host credential; keep it in the channel `.env`
  (host-local), not in the synced workspace.

## Writing your own relay

A minimal relay needs only: an authenticated queue behind `GET /v1/tasks`
(long-poll or return-immediately), an `ack` sink, a `results` sink, and a
heartbeat sink. The four endpoints above are the entire contract — anything that
implements them can drive Sutando.
