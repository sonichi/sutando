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

**`capabilities` (the poll's proof).** Every poll carries
`&capabilities=voice-outcome-v1`, the voice-outcome contract the heartbeat also
declares (below). One agent's credential may be used by more than one bridge, so
an AG2 Space broker serves a voice relay ask only to a poll that names the
contract itself, and serves a poll without it the tasks behind that ask; a relay
that predates the parameter ignores it.

**`ack_first` (acknowledging first).** An AG2 Space broker marks a voice relay ask
it serves with `"ack_first": true`, at most one per response. The bridge handles
a task served with that exact boolean before the rest of the response, on its
own, and publishes it to the core only once its ack is accepted:

1. It writes the task file into `tasks/.ack-first[.<instance>]/`, where neither
   the core (which reads `tasks/*.txt`) nor a task-file lookup looks, fsyncs that
   directory and `tasks/`, and commits a manifest entry. If any of these does not
   commit, nothing is staged and nothing is acked, so the broker serves the task
   again. Every later step resumes from the entry.
2. Before the ack it commits the task's `ack_first` mark, then the in-flight set.
   Until both commit, the task stays staged and unacked.
3. It acks the task with `durable: true` and the served `lease_id`, and decides:
   - `200` (also `"duplicate": true` for a repeat): accepted;
   - `404` with `"reason": "stale lease generation"`: refused. A newer serve holds
     the lease, and an acknowledged ask is never served again, so this serve was
     never accepted;
   - `404 {"error": "not leased …"}` without that reason, or any other answer:
     the bridge asks `GET /v1/tasks/<id>/status`. `withdrawn`, `dead` or
     `result_ready` (the ask ended, or was answered elsewhere) is refused;
     `acknowledged` after the plain `404` is accepted (no lease is left, so no
     other serve can hold that acknowledgement); anything else, an unreadable
     status, or `acknowledged` after an ambiguous answer keeps the task staged,
     and the ack is retried before every poll. Only the ack itself can say whose
     acknowledgement it is, so an ambiguous answer never publishes on the state.
4. Accepted: the acceptance is recorded in the entry, then the staged file is
   published to the core (a rename into `tasks/`), then both directories are
   fsync'd, and only then is the entry removed, so a power loss cannot bring the
   staged name back without the entry that says it was published. An acceptance
   that cannot be recorded keeps the task staged and unpublished; the next pass
   acks again. Before the publish, the `access_tier` the staged file froze is
   resolved again under the local policy in force now; if access has dropped, or
   the file cannot be read or parsed or names no known tier, nothing is
   published: the task stays staged and accepted, is reported under
   `ack_first_unresolved` (below), and is checked again on every pass.
   Refused: the decision is recorded in the entry first, then the staged file,
   the mark and the in-flight entry go, and the entry goes last, so a crash in
   between finishes the discard on the next pass. The task never reaches the core.

The manifest is `state/remote-task-ack-first-manifest[.<instance>].json`, keyed by
the local id; each entry names the broker id, the `lease_id`, the staged and
destination file names, a SHA-256 digest of the task file, and whether the ack
was accepted or refused. The bridge resumes each entry before every poll, so also
at start: a staged file whose acceptance is not recorded is acked again and
decided as above; one whose acceptance is recorded is published without a new ack.
A staged file that is gone was consumed by its publish: a task file with the same
digest, a claimed or archived copy, or a result for the task proves it, and the
entry is cleared without publishing again. The same proof beside an accepted
entry's staged file (a power loss kept the publish and brought the staged name
back) drops the staged copy instead of publishing it a second time. With neither, the entry is kept, the
bridge logs an error once, and it reports the entry under `ack_first_unresolved`
in `state/gateway-status[.<instance>].json` (the bridge's status file for a local
supervisor) for as long as it stays, together with any malformed entry and any
task still staged ten minutes after it was staged (an ack, mark, in-flight or
publish step that keeps failing). Such a task keeps being retried; the report
goes once it resolves. At start,
before the first poll, it also removes each file in its staging directory that no
entry names (a crash before the entry, inside a discard, or before a newer serve's
entry replaced an older one). A task served again before its ack replaces its
entry, so only the newest serve's lease is acked.

**The `ack_first` mark.** `state/remote-task-ack-first[.<instance>].json` maps the
broker id of each task served with `ack_first` to the local id it is published
under. It is a file of its own because an older bridge prunes the media sidecar
when it settles a task, and the mark must survive a restart: it decides whether
an honoured fold sends `consolidated_into` (see `POST /v1/results`). A mark is
removed once the broker has accepted that task's result and the result is
archived; at start when the outbox shows that result delivered and no result of
the task, or of a re-ask answering it, is still live (while the dedup alias
ledger cannot be read, no mark is retired at start); or when its ack is refused,
since such a task never reached the core and can never produce a result. An
older bridge never reads or writes the file. A task served without `ack_first`
is unchanged.

### `POST /v1/tasks/<id>/ack`

Claim/acknowledge a task so the server stops redelivering it.

```
body: { "id": "task-123", "lease_id": "<the served task's lease_id>", "durable": true }
```

The client acks each task as it is accepted. A server with at-least-once
delivery should treat ack as "stop redelivering"; the client is idempotent and
will not re-queue a task it already claimed or archived.

`durable: true` is sent only once the task file, its media sidecar and the
in-flight set are all fsync'd (for an `ack_first` task, the staged file and its
directory, its manifest entry and its mark too), so the task survives a crash of
this host. When a fresh queue write, its media sidecar, the in-flight set or the
pending-ack ledger does not commit, the client withholds the ack entirely rather
than claiming a task it could still lose. The flag is merely absent — a plain ack the
server should treat as "stop redelivering" and nothing more — when a redelivered
task is already queued and its durability could not be repaired. Acks that do
not confirm are persisted and retried; a per-task `404 {"error": "not leased …"}`
retires the retry for good, while a bare no-route 404/405 keeps the
endpoint-unsupported cooldown.
When the served task carries a `lease_id` (the broker's lease generation, fresh
on every serve), the ack echoes it — also on a retry after a restart, since the
pending-ack ledger keeps it — and a `404 {"reason": "stale lease generation"}`
retires that ack the same way; a task served without one is acked without the key.
An `ack_first` task is not in the pending-ack ledger: its manifest entry is its
retry record, because an accepted ack must also publish it, and its `404` is
decided with the task's status rather than retired (see `GET /v1/tasks`).

### `POST /v1/results`

Return a task's result.

```
body: { "id": "task-123", "body": "<result text>",
        "capabilities": ["voice-outcome-v1"], "dm_only": false }
body: { ..., "thread": "ask" }                                  // optional
body: { "id": "ask-…", "body": "", "consolidated_into": "ask-…",
        "capabilities": ["voice-outcome-v1"], "dm_only": false }  // an honoured fold
```

**The voice-outcome contract (`capabilities`, `dm_only`).** Every result carries
`"capabilities": ["voice-outcome-v1"]` and an explicit boolean `dm_only`: `true`
when the body carries `[dm-only]` anywhere (a standalone marker line is stripped
from `body`; an inline mention stays in it), `false` otherwise, a skip and a fold
included. An AG2 Space broker reads the two only for a voice relay ask, and treats
such an ask's result without them as private: an older bridge strips the marker
line and sends no `dm_only`, so its private answers still reach only the owner.
Both are part of the payload the outbox stores, so a retry, also after a restart,
resends them; a payload an older bridge stored carries neither. Every existing key
keeps its value, and a broker that predates the contract reads only the keys it
names, so it ignores both.

**`consolidated_into` (an honoured fold).** When the core folds a task whose
`ack_first` mark is kept (see `GET /v1/tasks`), or a re-ask answering one, into
another task (`[deduped: <holder>]`) and the dedup planner honours the fold, its
result closes with `consolidated_into`: the holder's **broker** id, resolved as a
result's own id is (the dedup alias first, then the instance prefix stripped), so
a named instance sends `ask-…`, never `task-<instance>~ask-…` or a re-ask's
`task-<hash>`. It carries an empty `body` and no `no_send`: the broker records the
ask as merged into the holder and posts nothing for it. The mark is on disk, so a
fold answered after a restart sends it too. While the alias ledger cannot be read,
or the marks cannot be read for a voice ask, the result waits for a later pass; a
holder that is not a task of this gateway is never named. Every other task, typed
and Signal tasks included, closes an honoured fold with the bare
`[deduped: <holder>]` and `no_send` as before, because the broker already acts on
`consolidated_into` for any task. A fold the planner re-asks or reports is
unchanged: the re-ask's answer goes under the original id, and the report is a
warning posted as the answer.

**`thread` (optional).** Defined by the broker in
[ag2-space/ag2space-backend#2138](https://github.com/ag2-space/ag2space-backend/pull/2138).
`"ask"` asks the broker to answer in a new thread rooted on the task's own asking
message; no field names an event id, so a result can only open a thread on the
message it was asked in. The broker:

- answers an ask already in a thread in that thread, with or without the field;
- ignores the field and posts a plain reply when the task has no source message,
  the body redirects with `[channel:]`, or the ask cannot root a thread;
- treats absent, `null` or `false` as today's placement;
- answers `400 {"error": "invalid thread: ..."}` for any other value and records,
  sends and completes nothing: the lease stays open.

A broker that predates the field ignores it, so a client may send it before the
broker supports it. The client sends only `"ask"`, and only on an ordinary
(non-suppressed, non-redirected) task result whose leading lines carry a bare
`[thread]` line (`src/result_markers.py`). Backend #2138 defines `"ask"` as valid,
so a `400` (or any other refusal) of a result carrying it is a bug to investigate,
not something the client degrades around: it parks like any other 4xx (below), with
no re-post. A retryable failure re-sends the stored payload, field included. The
field name and value live in one
place on the client: `RESULT_THREAD_FIELD` / `RESULT_THREAD_ASK` in
`packages/ag2-sparrow/ag2_sparrow/delivery_core/provider_ag2space.py`.

### `POST /v1/heartbeat`

Periodic liveness + capability ping.

```
body: {
  "client": "sutando-gateway-client",
  "protocol_version": 1,
  "provider": "<REMOTE_TASK_PROVIDER>",
  "tier": "<REMOTE_TASK_TIER>",
  "inflight": <int>,            // tasks currently claimed but not yet resulted
  "capabilities": ["task-ack", "heartbeat", "result-skip-markers", "core-status",
                   "team-collaborator", "voice-outcome-v1"]
}
```

`voice-outcome-v1` declares the voice-outcome contract: the poll's proof and
acknowledging an `ack_first` task before publishing it under `GET /v1/tasks`, and
`dm_only` and `consolidated_into` under `POST /v1/results`. An AG2 Space broker
keeps the declaration to decide whether a new call may use the voice relay; the
poll and every result carry the contract again, because a Mac can be downgraded
after the declaration was read.

`status` and `step` come from the core's
`state/core-status.json`, with the health-check overlay described under
[Independent health-check reporting](#independent-health-check-reporting). The
core's beat in the `health` row below decides when that self-report cannot; the first
matching row wins:

| `core-status.json` | `alive` | sent `status` |
|---|---|---|
| missing, or written before this host's graceful-stop tombstone `state/cores/<host>.stopped` | `false` or `null` | `offline` |
| present, under an hour old | any | the self-report |
| present, older than an hour | `true` | the self-report (an idle core rewrites nothing) |
| missing, malformed, or older than an hour | `false` (beat stale) | `offline` |
| missing or malformed | `true` | `idle` |
| missing, malformed, or older than an hour | `null` (no beat file) | `unknown` |

The tombstone is written by `core_heartbeat` on a graceful stop and deleted by its
next run, and a self-report written after it (a restart before the first beat)
supersedes it.

`core_heartbeat` deletes `.alive` when the core stops, so a missing beat vouches
for nothing. `unknown` is sent rather than omitting the field because the broker
keeps its last presence status when `status` is absent. A gateway with no
Sutando checkout (no `health` row) forwards the self-report unchanged, or nothing.

A relay that answers the heartbeat with `404` or `405` is retried after 60 s, doubling to at most
30 minutes, so a relay that deploys the route is picked up without a restart.

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
as the five-minute fallback. An expired report gives no opinion: the core status
passes through exactly as with no report, so a cached failure is never refreshed
and a host that stops running the checker is not pinned to `unknown`. Malformed,
empty, or future-dated reports emit `status: unknown`.
An absent report preserves legacy core-status reporting for installations
without the health checker. A passing report alone does not invent a core
status. If the gateway or host is down, no heartbeat can be sent; the broker's
existing contact timeout still yields disconnected/unknown.

The AG2 Space dashboard already classifies `error` as unhealthy, so this
requires updating Sutando's health checker and restarting its gateway bridge;
no dashboard schema change is needed. Reporting starts after the first health
check completes and follows the installed check cadence.
