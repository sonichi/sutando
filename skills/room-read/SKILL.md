# room-read — pull-on-demand room/channel history for an agent

Gives an agent a **read capability**: pull recent messages from a room/channel
*on demand*, so it can reference prior discussion instead of only seeing the
messages explicitly routed to it. This is the "agent as room participant"
upgrade — it moves an agent from a task inbox to something that can read
surrounding context.

**Usage**: `room_read.py <room_id> [--agent <mxid>] [--limit N] [--before <tok>]`

```bash
python3 skills/room-read/room_read.py '!roomId:hs' --agent '@my.agent:hs' --limit 20
```

Returns JSON: `{ok, room_id, reason, messages:[{sender, ts, body, event_id}]}`.
`ok:false` with a `reason` on any expected failure (gate-deny, no relay, 404/403,
network) — it never raises, so the caller degrades cleanly. As a shell tool the
CLI **exits 0 for any structured result** (a graceful `ok:false` "no context" is
not a failed task); usage errors exit 2 and unexpected bugs raise. `limit` is
clamped to `[1, MAX_LIMIT]` so a caller can't turn a context read into a huge
history pull.

## Design — why it's safe to add

It is **orthogonal to the task file bridge** (`tasks/` → `results/`). The async
task-in / result-out loop is untouched; this is a separate *synchronous pull*
the agent makes when it needs context. Nothing about how tasks arrive or how
results are delivered changes.

### Architecture boundary — the client speaks only the relay protocol

The local client never holds a platform/AppService token and never talks to a
homeserver directly. The layering is strict:

```
Matrix room
  <-> relay / broker / AppService     (platform-coupled, box-side)
  <-> /v1 agent relay protocol
  <-> remote-relay-bridge.py          (this client — provider-agnostic)
  <-> tasks/results file queue
  <-> agent core
```

So this skill has a **single backend**: the generic relay verb
`GET {RELAY_URL}/v1/rooms/{room}/messages?limit=N`. Whether the relay backs that
verb with a bot-client CS-API read or an AppService masquerade is the relay's
(box-side) concern, invisible here. The AppService is AG2-side infrastructure
that mints/puppets per-agent virtual users; **the agent never sees its token.**
The client stays provider-agnostic and portable (any self-hoster running it gets
reads with no platform creds locally).

### Scope gating — membership is the consent gate, enforced relay-side

Read requires the agent be a **joined member** of the room — not an out-of-band
privileged peek. This is the consent model: a joined agent shows up in the member
list (humans see it's present), and it only reads rooms it was deliberately added
to. Read-without-join is an AppService power that fits *bridging*, not a
transparently-participating agent.

**Enforcement is relay-side and authoritative:** the relay verb denies a
non-member read (`403`); the AppService cannot bypass this. The client offers an
*optional* local default-deny pre-filter (`ROOM_READ_GATE`) as defense-in-depth,
but it is **not** the security boundary:

- No gate file present → `load_gate()` returns `None` → the client does not
  pre-filter; it defers entirely to relay enforcement.
- Gate file present → default-deny: an agent reads only if opted in via
  `"rooms": [...]` (explicit) or `"all_member_rooms": true`. See
  `room-read-gate.json.example`.

### Graceful degrade

Missing relay config, gate-deny, network error, or a non-2xx response (relay
doesn't implement the verb → 404; non-member → 403) all return `ok:false` with a
reason and an empty `messages` list. Additive and versioned: existing deployments
that don't configure it, and relays that don't implement the verb, are unaffected.

## Configuration (no platform literals in the code)

The client only needs the relay coordinates — no homeserver, no AppService token:

| env | meaning |
| --- | --- |
| `RELAY_URL` / `REMOTE_TASK_URL` | relay base for the read verb |
| `RELAY_TOKEN` / `REMOTE_TASK_TOKEN` | bearer for the relay (optional) |
| `AGENT_MXID` | the agent identity to read for (relay resolves membership) |
| `ROOM_READ_GATE` | absolute path to the optional client gate JSON (e.g. `<workspace>/state/room-read-gate.json`); falls back to `room-read-gate.json` in cwd |

The AppService token lives **only on the box** (relay/broker side) and never in
this repo, a task file, or the local vault — per the architecture boundary above.

## Tests

`python3 skills/room-read/test_room_read.py` — 22 unit tests covering
the client gate (defer-to-relay / default-deny / opt-in), the relay-verb read,
normalisation, and graceful degrade (incl. 404/403 → no-op). No network.

## Status

- Client tool + optional gate + relay-verb backend + tests: done (this skill).
- Relay-side `GET /v1/rooms/{room}/messages` (the box-side verb, membership-
  enforced) + live e2e: the paired half, tracked in the parity epic.

## Where this fits: the agent-capability parity epic

This skill is **one slice** of a larger goal — an agent on a Matrix/relay
deployment must be **no worse than a chat bot-client (e.g. Discord)** on every
capability axis, and surpass it where Matrix uniquely allows. Room-read closes
the read-history gap (the one axis that was below the floor). The remaining
slices, tracked as the epic's acceptance checklist:

| Agent capability | bot-client baseline | status |
| --- | --- | --- |
| Receive addressed message (DM/@mention) → task | yes | ✅ relay inbound |
| Reply | yes | ✅ relay `POST /v1/results` |
| **Read room history (joined rooms)** | yes (`src/discord-read.py`) | 🟡 **this skill** |
| Proactive post | yes | ✅ relay room-op |
| Access tiers (owner/team/other) | yes | ✅ relay `access_tier` |
| Membership / presence | yes | ✅ Matrix membership |
| **File / media in+out** | yes (`att.save` / `[file:]`) | ❌ relay is text-only today → add `m.file`/`m.image` |
| **React to a message** | yes (`add_reaction`) | ❌ add `m.reaction` via the same verb pattern |
| **Delivery/routing markers** (`[channel:]`, dedup/`[no-send]`) | yes | ❌ relay needs equivalents |
| Message edit → reprocess | yes (`m.replace`) | ❌ surface edit events |
| Durable idempotency (event_id dedup, txn_id, delivered sentinel) | yes | ❌ add |

**Surpass (Matrix-only headroom):** per-agent virtual identities (one human →
many agents), namespace event firehose, custom typed state events (task
boards/agent policy/vault), Spaces, federation, E2EE, embedded widgets — these
are why the platform can be *better* than a chat bridge, not just at parity.
