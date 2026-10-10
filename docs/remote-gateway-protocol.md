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
| `REMOTE_TASK_ON_TASK` | no | — | Wake command run once per newly queued task file. Split with `shlex` and run without a shell, the file path appended as the last argument; `SPARROW_TASK_ID` and `SPARROW_TASK_FILE` are set and every relay credential is removed from its environment. Sparrow does not wait for it, kill it, or retry it; a non-zero exit or a start failure is logged once. |
| `REMOTE_TASK_TIER` | no | `owner` | Local access tier stamped on every inbound task; `owner` for the personal-agent model, set `team`/`guest` for a shared gateway (see Security); `other` is accepted as the legacy spelling of `guest`. |
| `REMOTE_PROACTIVE_ROOM` *(.env too)* | no | — | The SYSTEM destination only. Owner-directed `results/proactive-*.txt` nudges and runtime prompt cards go to the gateway's `owner_dm_room` for this agent (`GET /v1/agents`), a reading kept identity-bound on disk (`state/owner-routing.json`) across restarts and outages and never replaced by an answer without one; with no reading yet they are HELD (file left in place, retried every 30 s, logged), never sent here. Unset → read from this instance's `channels/<dir>/.env`. A file naming its own room (`[channel: !room]`) never needs it; the drain runs whether or not it is set. |
| `REMOTE_ALERT_ROOM` | no | none (gateway alert disabled) | Explicit owner-only room id for core-independent health alerts sent by the launchd fallback. Never inferred from last activity because that room may be shared. |

**Use the split form** (`REMOTE_TASK_URL` + `REMOTE_TASK_TOKEN`) — it's the recommended way to configure the bridge.

> **Legacy / bootstrap shortcut:** the bridge also accepts a *combined* token of the form `REMOTE_TASK_TOKEN="https://relay.example.com|<secret>"` (URL and secret joined by `|`), which it splits at startup. This exists only so a one-shot onboarding string can carry both halves. If you use it, **quote it in `.env`** — an unquoted `|` is a shell pipe when the file is sourced. Prefer the split form for anything persistent.

Older installs using `AG2_REMOTE_URL` / `AG2_REMOTE_TOKEN` remain supported by
both the bridge launcher and the core-independent health-alert sender during
the compatibility window.

## Transport

The orphan-result sweep leaves local tasks with a pre-body `source: cron`
header untouched, including archived tasks and old completion files. A local
scheduled task has no gateway lease to close. Its completion does not become a
remote reply; owner notifications use the separate proactive delivery path.
Gateway task results retain the existing recovery, suppression, and retry behavior.

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

A worker-picker button may be sent as `"picker_command"` (`add` | `pin` |
`unpin`) plus optional `"picker_args"` — a JSON object, either inline or
already serialized as a string; the bridge writes it as JSON either way. Both
are written as trusted pre-body headers, because `worker_picker_commands.py`
reads them with the parser that stops at `task:`. A command the reader cannot
honour — an unknown verb, args that are not an object, or arguments that do not
fit the verb — is refused by name rather than resolved from the sentence, and
the room always comes from `channel_id`, never from `picker_args`. A broker
that sends no `picker_command` keeps the prose fallback.

An AG2 Space broker may additionally send `"session_scope": "room"`. The
bridge writes only that exact value as a trusted pre-body header; missing,
unknown, or malformed values are omitted, preserving the main-session path for
older brokers, bridges, and Sutando installations. An optional task handler may
use the header with `source: ag2space` and `channel_id` to select a durable
room-specific provider session.

### `POST /v1/tasks/<id>/ack`

Claim/acknowledge a task so the server stops redelivering it.

```
body: { "id": "task-123", "durable": true }
```

The client acks each task as it is accepted. A server with at-least-once
delivery should treat ack as "stop redelivering"; the client is idempotent and
will not re-queue a task it already claimed or archived.

`durable: true` is sent only once the task file, its media sidecar and the
in-flight set are all fsync'd, so the task survives a crash of this host. When a
fresh queue write, its media sidecar, the in-flight set or the pending-ack
ledger does not commit, the client withholds the ack entirely rather than
claiming a task it could still lose. The flag is merely absent — a plain ack the
server should treat as "stop redelivering" and nothing more — when a redelivered
task is already queued and its durability could not be repaired. Acks that do
not confirm are persisted and retried; a per-task `404 {"error": "not leased …"}`
retires the retry for good, while a bare no-route 404/405 keeps the
endpoint-unsupported cooldown.

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
  "capabilities": ["task-ack", "heartbeat", "result-skip-markers", "core-status", "team-collaborator"]
}
```

`team-collaborator` tells the AG2 Space control plane that this gateway
understands the per-agent Collaborator control layered over Team. Gateways
without it safely keep Team on their prior restricted path.

When the gateway runs inside a Sutando checkout it adds the core's health row
from [`GET /health`](health-snapshot.md) and the `worker_health.v1` capability:

```
"health": {"alive": true|false|null, "motion": "idle|moving|unknown",
           "condition": "healthy|abnormal|unknown", "reason": "<slug>"|null,
           "since": <unix seconds>|null}
```

`reason` is a slug of `[a-z0-9-]{1,40}`, not a closed set. `since` is for display;
the broker times freshness by when it received the heartbeat. A change in the row
sends the heartbeat at once, without waiting for the interval. A gateway with no
Sutando checkout around it sends neither field.

### `POST /v1/workers` *(optional)*

The worker pool this gateway fronts, pushed when the local advertisement's
content changes and re-sent unchanged every 600 s (every 20 s when it carries
health, see below), so a relay that restarted with an empty copy heals without an operator. Sent only when the gateway finds a readable
advertisement; a gateway with no pool never calls it.

```
body: {
  "roster_version": <int>,           // monotonic per publisher
  "live_cores": ["<worker id>", …],  // ids currently serving
  "dead_cores": ["<worker id>", …],
  "bindings": { "<room id>": "<worker id>" },  // rooms the owner pinned
  "ts": <unix seconds>               // when the publisher compiled it
}
success: 2xx, body ignored
```

When the advertisement carries the per-worker report (`workers: [{id, state, …}]`),
a gateway that sends `worker_health.v1` adds each non-retired worker's `health`
row (the heartbeat's shape) and a top-level `suspended: {"reason", "at"} | null`,
set while the owner has quit the app and the pool is paused. A health change
pushes the report again even when the advertisement has not changed.

A report carrying health is also re-sent, changed or not, whenever the last one
is 20 s old, checked between polls. That keeps gaps under 60 s: the broker requires
a health report at least every 60 s and marks worker rows stale 120 s after the
last one it received. A body without health (the legacy snapshot, or a standalone
sparrow with no health snapshot) keeps the 600 s re-send.

### `PUT /v1/agents/<mxid>/profile` *(optional)*

The instance's identity card, pushed on the same change signal as the workers
snapshot and from the same single read, so the two can never describe different
revisions. Unchanged, it is re-sent every 600 s, not at the workers' 20 s refresh. `<mxid>` is percent-encoded as one path segment.

```
body: {
  "display": { "name": "<display name>" },
  "host":    { "host_id": "<short hostname>", "kind": "local" },
  "workers": { "<worker id>": { "label": "<name>", "runtime": "<runtime>" } }
             // label always; runtime when the publisher knows it
}
success: 2xx, body ignored
```

The broker REPLACES the profile document, so the gateway sends this only from
an advertisement it could read in full.

### `GET /v1/agents/<mxid>/profile` *(optional worker labels)*

After a successful profile PUT, the Sutando adapter may read the owner's
display-name overrides for its workers. The broker response must identify the
same `<mxid>` and provide `schema_version: 1`, a nonnegative integer
`config.version`, and `display.worker_labels` as a map from stable worker IDs
to nonempty display names. An empty map explicitly clears all overrides.
`workers[]` in this response is an effective presentation projection; the
adapter never treats it as rename intent.

Sutando stores these names separately from its own routing aliases. The
worker ID remains the routing and attribution key, and clearing an owner
override reveals the original local label. The read and local application run
off the task poll path. An unsupported, unavailable, or invalid response keeps
the last local state and cannot delay task delivery.

**Unsupported is not an error.** A relay that does not implement an optional route
answers `404`, `405` or `501`; the gateway logs once and stops trying for an
hour. Any other failure (5xx, timeout, transport) is retried in five minutes.
These calls cannot fail the task loop: all run in a background thread
AFTER the beat's durable retries, so the next `/v1/tasks` poll is issued while
a slow push is still in flight and an optional push never delays an
owner-approved publication. A push still running when the next beat arrives is
left to finish; that beat's push is skipped, not queued.

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

Outbound `[file: …]` markers upload through `POST /v1/rooms/<room>/media`. A
task the server marked with a `signal` object instead uploads against its own
lease, `POST /v1/tasks/<id>/media` with `{ "ordinal": 0..9, "filename":
"<name>", "content_b64": "…" }`, where `<id>` is the id the result is delivered
under and `ordinal` is the marker's position in the body. The client records
that mode in `state/remote-task-media[.<instance>].json` before the task is
queued, so it survives a restart. `409` (content conflicts with an upload the
server already recorded) and `423` (encrypted room) are reported in-band and
never retried; a network failure or any 5xx defers the whole result, and the
retry re-offers the same id, ordinal and bytes so the server can resume rather
than store a second copy.

## Delivery + idempotency

- Delivery is assumed **at-least-once**. The client persists its in-flight set
  and restores it across restarts, so a task redelivered after a crash is not
  run twice.
- A task whose `id` is already queued, claimed, or archived locally is dropped
  (idempotent write).

### Recovering result POSTs after a gateway outage

Ordinary task results use the shared outbox and a persisted retry schedule.
The first claim starts a **10-minute elapsed window**. Retry delays are
**2, 4, 8, 16, then 30 seconds**, capped at 30 seconds thereafter. These are
minimum delays: the outbound drain's polling cadence and request duration can
make an attempt later. Each eligible drain makes one result POST; an ambiguous
response defers the idempotent resend to the next scheduled attempt. The answer
and broker result ID remain the same. No agent task is created to regenerate
an answer because its POST failed.

HTTP 401/403 (while polling recovers authentication), 408, 425, 429, 5xx and
transport failures are retryable. Other 4xx responses,
malformed envelopes and explicit decline envelopes are permanent refusals and
park on the first attempt. This policy applies to the gateway **task-result**
leg only; proactive room sends and other providers keep their existing retry
policy. Providers without an idempotent-send or reconciliation capability still
park an ambiguous outcome rather than retrying it automatically.

The outbox item stores its start, absolute deadline, next eligible time and
failure count atomically under the delivery claim lock. A restart retains these
values; time spent stopped consumes the window. At least five failed sends are
required before expiry parks an answer, so sparse orphan sweeps and laptop sleep
do not reduce recovery to a single attempt. Scheduling uses Unix wall time; clock
adjustments can shorten or extend the window, while the minimum remains intact.
A crashed claim owner is recovered by the existing owner-liveness/TTL protocol
(up to its 300-second reclaim delay), without stealing an active sender's claim.
An attempt already in progress at the deadline can finish. After the deadline,
remaining minimum attempts still use backoff; further failures park the answer. Invalid schedule state parks visibly instead of
silently granting a fresh budget.

The gateway outbox preserves an accepted record when an unarchived result is
seen again after a crash; it does not start another answer delivery cycle.
If the broker redelivers an accepted task, the bridge re-ACKs it and POSTs a
structured `no_send` lease-close control through a separate outbox item. The
original accepted answer and receipt stay intact. Repeated redeliveries can
start another control cycle; failed controls retain the usual bounded retry
schedule. The broker must finalize duplicate results as well as fresh results
for this control to clear a lingering lease. Retries use
the originally published payload even if the caller rebuilds different text.
Accepted re-ask aliases remain available so waiting dependents resolve the
holder's broker receipt after its result is archived. Abandoned torn claims
use the outbox's existing grace-period sweep; fresh torn claims remain guarded.

Success means **accepted by the gateway**, including result-ID deduplication and
closing its task lease. It does not establish downstream Matrix delivery.
The outbox's historical `DELIVERED` label represents gateway acceptance for this
provider. Logs explicitly say `accepted by gateway; Matrix delivery unconfirmed`.
Pending logs include the next attempt and deadline; terminal records distinguish
`retry-window-exhausted`, `permanent-refusal` and `invalid-retry-state`.

After exhaustion or permanent refusal, the bridge retains the answer under
`results/undelivered/` and logs the outbox recovery command. An operator can
inspect the record and deliberately recover it with:

```sh
ag2-sparrow-outbox --root <results>/.outbox<instance-suffix> inspect <broker-result-id>
ag2-sparrow-outbox --root <results>/.outbox<instance-suffix> requeue <broker-result-id> \
  --reset-attempts --results-dir <results> --body-id <local-task-id>
```

`--reset-attempts` resets both the attempt count and elapsed retry schedule.
Without it, an expired window stays expired. The operator action increments the
outbox resend epoch, but the gateway envelope retains the same broker result ID.
Inspect whether a reply was already manually sent before recovering it.
Existing parked records and historical quarantined replies are **not** migrated
or replayed automatically. Live nonterminal records without a schedule acquire
one on their next claim.

### Duplicates waiting for a holder's answer

Gateway dedup recovery distinguishes an existing pending answer from gateway
acceptance and failure. A compatible duplicate waits while its holder's answer
is pending. Its dedup result and original in-flight ID persist until the holder
is accepted, then its own broker lease closes with a suppressed result POST.
The holder's existing outbox item provides delivery ownership and retry timing;
concurrent dependent decisions do not create recovery aliases or additional
agent tasks. The same durable files and receipt record reconstruct this state
on restart. Archive location alone is not evidence of gateway acceptance.

Compatibility requires matching task source, sender and room with readable
holder provenance. A redirected holder answer cannot silently satisfy the
original room. Cross-room and cross-sender dedups retain the bounded re-ask and
report path. A genuinely missing or unusable holder also keeps the existing
one-re-ask-per-lineage limit.

A quarantined, exhausted or otherwise unverifiable existing answer requires
operator recovery. Dependents receive a visible recovery report under each of
their own broker IDs; they do not regenerate or replay the holder's answer.
Report delivery itself uses the bounded gateway retry policy. An exhausted
report remains retained for operator inspection. Acceptance is the strongest
holder evidence available through the current result POST contract; this does
not promise downstream Matrix delivery.

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
- **A room named by the voice client is a claim, not a destination.** The
  desktop's in-room voice session announces its room with a `session.context`
  frame (handled by the optional `skills/ag2space-voice/` plugin, which writes the
  request), but only the gateway can prove membership. The bridge answers
  `state/voice-room-checks/<key>.request.json` with `<key>.verdict.json`
  (`src/voice_room_membership.py`): `verified` only when `/v1/room`
  `{"op": "members"}` lists BOTH the agent and the owner from `GET /v1/agents`;
  an unreadable room, a missing identity or no bridge at all is a refusal and
  the session stays on the owner DM. The same verdict gates the claim of a
  voice result, and only this shape: a file named
  `results/proactive-result-*.to-ag2space.txt` whose body's `[channel: !room]`
  redirect names a Matrix room. An unverified room's file is left in place,
  logged once per hold, and re-checked on every scan; it is released when the
  room verifies and is never posted or rerouted to the owner DM before that,
  with no age limit. Nothing else is ever held by this check: an untagged
  `proactive-result-*.txt` (whatever its body opens with), a tagged file with
  no room line or a skip marker, and any other `proactive-*.txt` follow the
  ordinary claim rules. Verdicts are cached 60 s per room on both sides;
  an unusable owner reading is retried after 5 s, not per scan.

## Writing your own relay

A minimal relay needs only: an authenticated queue behind `GET /v1/tasks`
(long-poll or return-immediately), an `ack` sink, a `results` sink, and a
heartbeat sink. The four endpoints above are the entire contract — anything that
implements them can drive Sutando.


### Independent health-check reporting

Each completed `health-check.py` run atomically publishes a compact
`state/agent-health.json` record (`version`, `checked_at`, `total`, `failures`).
It uses the same failure predicate as the local check's exit code. Warnings
remain warnings; check names, diagnostic output, task text, and paths are not
included in the record.

The gateway overlays a recent failing report onto its next heartbeat as
`status: error` with a failure count. This works even without a core status
file, allowing the independent health checker to report a failed core.
A later passing check removes the override and resumes the core's status.
Repair attempts do not imply recovery: another completed check must verify it.

Reports expire after 35 minutes, allowing the app's 30-minute cadence as well
as the five-minute fallback. Expired, malformed, or empty reports emit
`status: unknown`, preventing cached health from being refreshed indefinitely.
An absent report preserves legacy core-status reporting for installations
without the health checker. A passing report alone does not invent a core
status. If the gateway or host is down, no heartbeat can be sent; the broker's
existing contact timeout still yields disconnected/unknown.

The AG2 Space dashboard already classifies `error` as unhealthy, so this
requires updating Sutando's health checker and restarting its gateway bridge;
no dashboard schema change is needed. Reporting starts after the first health
check completes and follows the installed check cadence.
