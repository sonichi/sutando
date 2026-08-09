# Remote gateway protocol

`src/remote-gateway-bridge.py` lets a remote HTTP server dispatch tasks to a local
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
| `REMOTE_TASK_TIER` | no | `owner` | Local access tier stamped on every inbound task; `owner` for the personal-agent model, set `team`/`other` for a shared gateway (see Security). |
| `REMOTE_ALERT_ROOM` | no | none (gateway alert disabled) | Explicit owner-only room id for core-independent health alerts sent by the launchd fallback. Never inferred from last activity because that room may be shared. |

**Use the split form** (`REMOTE_TASK_URL` + `REMOTE_TASK_TOKEN`) — it's the recommended way to configure the bridge.

> **Legacy / bootstrap shortcut:** the bridge also accepts a *combined* token of the form `REMOTE_TASK_TOKEN="https://relay.example.com|<secret>"` (URL and secret joined by `|`), which it splits at startup. This exists only so a one-shot onboarding string can carry both halves. If you use it, **quote it in `.env`** — an unquoted `|` is a shell pipe when the file is sourced. Prefer the split form for anything persistent.

Older installs using `AG2_REMOTE_URL` / `AG2_REMOTE_TOKEN` remain supported by
both the bridge launcher and the core-independent health-alert sender during
the compatibility window.

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
  "client": "sutando-gateway-client",
  "protocol_version": 1,
  "provider": "<REMOTE_TASK_PROVIDER>",
  "tier": "<REMOTE_TASK_TIER>",
  "inflight": <int>,            // tasks currently claimed but not yet resulted
  "capabilities": ["task-ack", "heartbeat", "result-skip-markers", "core-status"]
}
```

## Media markers (optional)

Instead of raw bytes, a gateway may hand the task body a media marker:

    [<tag>: <url> mime=<mime> name=<filename> size=<bytes> kind=<msgtype>] <caption>

The client resolves it locally: downloads the bytes (default 25 MB cap) and
rewrites the marker to `[File attached: <local path>]` (`[Photo attached: …]`
for `kind=m.image`) — the same inbound convention the other bridges use. Any
failure leaves the marker untouched.

Config: `REMOTE_MEDIA_MARKER` (tag, default `remote-media`),
`REMOTE_MEDIA_HS_TOKEN` + `REMOTE_MEDIA_HS_ORIGIN` (homeserver bearer and the
exact origin it may be sent to), `REMOTE_MEDIA_DIR`, `REMOTE_MEDIA_MAX_BYTES`.

Credential routing is by parsed exact origin, never string matching:

- gateway bearer → only when the URL's scheme/host/port equal the gateway's
  AND the path sits at/under the gateway base path with a `/` boundary;
- homeserver bearer → only for `/_matrix/` paths on exactly
  `REMOTE_MEDIA_HS_ORIGIN` (legacy media routes are upgraded to the MSC3916
  authenticated route first); unset origin ⇒ Matrix media is never credentialed;
- anything else → fetched with no credentials.

Authenticated fetches refuse redirects (a 3xx is a failure), so a
gateway-controlled URL can never bounce a bearer to another host.

## Delivery + idempotency

- Delivery is assumed **at-least-once**. The client persists its in-flight set
  and restores it across restarts, so a task redelivered after a crash is not
  run twice.
- A task whose `id` is already queued, claimed, or archived locally is dropped
  (idempotent write).

## Security

- Inbound tasks are **not trusted to set their own access tier.** The bridge
  stamps every task with the local `REMOTE_TASK_TIER` as the last `access_tier:`
  line, so a task body cannot forge a higher tier. **Default is `owner`** for the
  personal-agent model (2026-07-08): the gateway authenticates with its owner's
  own bearer and the broker owner-scopes every pull, so its tasks are the
  owner's own (e.g. voice delegations); trust derives from the broker's
  owner-scoping, not from the gateway process or the task's claim. A **shared /
  multi-user gateway** (one that could pull tasks not scoped to a single owner)
  MUST set `REMOTE_TASK_TIER=team` (or `other`) explicitly. An invalid value
  fails **closed** to `team`.
- The token is a per-host credential; keep it in the channel `.env`
  (host-local), not in the synced workspace.

## Writing your own relay

A minimal relay needs only: an authenticated queue behind `GET /v1/tasks`
(long-poll or return-immediately), an `ack` sink, a `results` sink, and a
heartbeat sink. The four endpoints above are the entire contract — anything that
implements them can drive Sutando.
