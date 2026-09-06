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

The bridge reads these from the environment. `channels/<provider>/.env` is not
sourced by every launcher, so the bridge also reads that file directly for the
keys marked *(.env too)* below — an exported value always wins.

Those file reads happen **at import**, so a `.env` edit needs a bridge restart
to take effect (unlike `REMOTE_TASK_TOKEN`, which is re-read on rotation). In
the file as in the environment, **presence decides, not truthiness**: a key
written with an empty value is an explicit "off" and does not fall through to a
lower-precedence candidate.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `REMOTE_TASK_URL` | yes | — | Relay base URL (e.g. `https://relay.example.com`). |
| `REMOTE_TASK_TOKEN` | yes | — | Bearer token sent on every request. |
| `REMOTE_TASK_PROVIDER` | no | `remote` | Label written as a task's `source:` when the task omits one. |
| `REMOTE_TASK_POLL_WAIT` | no | `25` | Long-poll seconds requested per `/v1/tasks` call. |
| `REMOTE_TASK_TIER` | no | `owner` | Local access tier stamped on every inbound task; `owner` for the personal-agent model, set `team`/`other` for a shared gateway (see Security). |
| `REMOTE_PROACTIVE_ROOM` *(.env too)* | no | — | Default room id to deliver `results/proactive-*.txt` nudges to (`POST /v1/room` op:message, claim-by-rename, archive on success). Unset → read from this instance's `channels/<dir>/.env`; still unset → proactive files are not scanned. Exported as empty → stays empty, which is how a named secondary gateway keeps nudges on the primary. Deliberately explicit — never auto-learned from task channel_ids, since a nudge may be owner-private. Result-body markers are honored via the shared parser (`result_markers.parse_markers`): a `[channel: !room:server]` first line redirects that one nudge, `[dm-only]` suppresses any redirect (nudge stays here), skip markers archive silently, and a foreign `[channel:]` destination (Discord/Slack id) leaves the file to its own bridge. |
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

A task object **must** carry a unique `"id"`. Recognized string fields
(`task`, `source`, `channel_id`, `user_id`, `priority`, …) are newline-confined
and written into the local task file the core consumes. For AG2 Space, the
broker also supplies its room-policy `access_tier` attestation.

#### Worker pins are control messages, and `source` is what says so

A broker may send a **worker pin** — a control message that binds a room to a
worker rather than work to be executed. The bridge consumes a pin as control
**only** when the task carries the broker's authoritative stamp
`"source": "worker-picker"`. That stamp is the sole discriminator.

The pin's `id` shape (`worker-pin-<digits>-<hex>`) is **not** a discriminator
and must never be used as one. A task object is only required to carry a
unique `id`, and `source` is optional, so an ordinary task is free to arrive
with an id of any shape and no `source` at all. A classifier keyed on the id
would consume such a task as control: journal it, ACK it — which stops
redelivery — and close its lease with `[no-send]`, destroying owner work
silently.

**Compatibility:** a pin-shaped id arriving *without* the stamp is written as
an ordinary task. That is the fail-safe direction — a pin mistaken for work is
visible and recoverable, whereas work mistaken for a pin is destroyed. A bridge
must not fall back to id-only consumption for older brokers, and must not gate
on process-local state such as "a stamped pin was seen earlier": that resets on
restart, so the destructive path re-arms on every start.

An AG2 Space broker may additionally send `"session_scope": "room"`. The
bridge writes only that exact value as a trusted pre-body header; missing,
unknown, or malformed values are omitted, preserving the main-session path for
older brokers, bridges, and Sutando installations. An optional task handler may
use the header with `source: ag2space` and `channel_id` to select a durable
room-specific provider session.

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

`metadata` (`worker_id`, `location`) is an OPTIONAL extension to this envelope.
The client sends it only after the broker advertises `worker-metadata` in its
heartbeat reply, so a relay that rejects unknown keys keeps receiving exactly
`{id, body}`. Result attribution is therefore absent, not lost, against a broker
that does not advertise it.

### `POST /v1/heartbeat`

Periodic liveness + capability ping.

```
body: {
  "client": "sutando-gateway-client",
  "protocol_version": 1,
  "provider": "<REMOTE_TASK_PROVIDER>",
  "tier": "<REMOTE_TASK_TIER>",
  "inflight": <int>,            // tasks currently claimed but not yet resulted
  "capabilities": ["task-ack", "heartbeat", "result-skip-markers", "core-status", "team-collaborator"]
}
```

The reply may advertise the broker's own capabilities:

```
reply: { "capabilities": ["worker-metadata"] }
```

`worker-metadata` tells the client the broker accepts the optional `metadata`
key on `POST /v1/results`. Brokers that omit it receive the documented envelope.

`worker-routing` is a **separate** capability and is not implied by
`worker-metadata`: a broker may accept result attribution without wanting to be
routed to. It tells the client the broker accepts seat identity on three request
bodies — `worker=` on `GET /v1/tasks`, and `worker_id` + `location` on both
`POST /v1/heartbeat` and `POST /v1/workers`.

Enable/revoke is a state machine driven entirely by the heartbeat, because the
heartbeat reply is the only capability channel:

| event | state |
|---|---|
| reply advertises `worker-routing` | enabled from the NEXT request onward |
| reply omits it | revoked |
| heartbeat 404/405 (no endpoint) | revoked, and the heartbeat is disabled |
| other non-auth HTTP error | revoked |
| network error / timeout | revoked |
| malformed 200 (undecodable JSON) | revoked |
| truncated 200 (`IncompleteRead`) | revoked |
| 401/403 | raises; not a capability signal |

Absence of a reply is absence of evidence, so every non-advertising outcome
revokes. The asymmetry is deliberate: dropping the keys cannot lose a result,
whereas keeping them against a strict broker can. The advertising heartbeat is
itself legacy-shaped — identity rides only the requests that follow it.

`team-collaborator` tells the AG2 Space control plane that this gateway
understands the per-agent Collaborator control layered over Team. Gateways
without it safely keep Team on their prior restricted path.

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

- Inbound message text is **not trusted to set its own access tier.** Effective
  access is the lower of the broker-attested room tier and the owner-controlled
  local cap (`REMOTE_TASK_TIER`, optionally narrowed per sender by
  `channels/ag2space/access.json` `tierMap`). Missing or invalid broker values
  fail closed to Guest. Every serialized wire field is newline-confined, and the
  bridge emits the resolved `access_tier:` independently, so message text cannot
  forge a higher tier.
- A broker-attested AG2 Space **Team** tier plus exact boolean
  `collaborator: true` is the explicit trusted-runtime opt-in. The legacy wire
  tier remains Guest with `requested_access_tier: team`, so older gateways stay
  restricted. A capable bridge promotes that signed combination and adds one
  `collaborator: true` line before the task body. A local owner-to-Team cap does
  not opt a room in; missing/malformed controls fail closed. This setting is
  controlled per room and per agent rather than by a host-wide environment flag.
- Collaborator result secret scanning defaults on. An exact broker boolean
  `sensitive_data_filter: false` adds one trusted pre-body opt-out stamp; missing,
  malformed, duplicated, or body-authored values keep scanning enabled. The
  delivery-control-marker guard remains active even when secret scanning is off.
- The default local cap remains `owner` for the personal-agent model. A shared /
  multi-user gateway SHOULD set a lower local cap as defense in depth. Invalid
  local cap values fail closed to Guest.
- The token is a per-host credential; keep it in the channel `.env`
  (host-local), not in the synced workspace.

## Writing your own relay

A minimal relay needs only: an authenticated queue behind `GET /v1/tasks`
(long-poll or return-immediately), an `ack` sink, a `results` sink, and a
heartbeat sink. The four endpoints above are the entire contract — anything that
implements them can drive Sutando.
