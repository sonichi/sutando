# room-read — pull-on-demand room/channel history for an agent

Gives an agent a **read capability**: pull recent messages from a room/channel
*on demand*, so it can reference prior discussion instead of only seeing the
messages explicitly routed to it. This is the "agent as room participant"
upgrade — it moves an agent from a task inbox to something that can read
surrounding context.

**Usage**: `room_read.py <room_id> [--agent <mxid>] [--limit N] [--backend generic|appservice]`

```bash
python3 skills/appservice-room-read/room_read.py '!roomId:hs' --agent '@my.agent:hs' --limit 20
```

Returns JSON: `{ok, backend, room_id, reason, messages:[{sender, ts, body, event_id}]}`.
`ok:false` with a `reason` on any expected failure (gate-deny, no backend,
404/403, network) — it never raises, so the caller degrades cleanly.

## Design — why it's safe to add

It is **orthogonal to the task file bridge** (`tasks/` → `results/`). The async
task-in / result-out loop is untouched; this is a separate *synchronous pull*
the agent makes when it needs context. Nothing about how tasks arrive or how
results are delivered changes.

### Two backends, one interface

Selected by `ROOM_READ_BACKEND`, else auto-detected from which credentials are
present:

| backend | how it reads | privilege | for |
| --- | --- | --- | --- |
| `generic` | `GET {RELAY_URL}/v1/rooms/{room}/messages?limit=N` | none (relay maps the generic verb to whatever platform it fronts; a bot-client read bounded by membership) | **portability** — self-hosters / non-Matrix providers get reads with no platform creds on the client |
| `appservice` | `GET {HOMESERVER}/_matrix/client/v3/rooms/{room}/messages?user_id={mxid}&dir=b&limit=N` (Bearer `AS_TOKEN`) | privileged server-side: masquerade as the agent, read without joining, full CS API | **the rich Matrix path** |

Keeping the **generic** verb alongside the **appservice** path is deliberate:
the generic verb is the provider-agnostic, Discord-bot-equivalent layer that
works everywhere; the AppService is the Matrix-only extra reach. Not either/or.

### Per-agent scope gating (opt-in, never blanket)

Full-room read is the powerful, risky part, so it is **default-deny**. An agent
may read a room only if a gate config opts it in. Config: JSON at
`ROOM_READ_GATE` (default `<workspace>/state/room-read-gate.json`) — see
`room-read-gate.json.example`:

- `"rooms": [...]` — explicit allowed room ids for that agent.
- `"all_member_rooms": true` — any room the agent is a member of (the backend
  still enforces membership; the gate is the opt-in layer on top).

An agent not present in the gate file reads nothing. The gate is checked
**before** any backend call, so a denied agent never reaches the network.

### Graceful degrade

Missing creds, gate-deny, unknown backend, network error, or a non-2xx response
(a relay that doesn't implement the verb → 404; a non-member masquerade → 403)
all return `ok:false` with a reason and an empty `messages` list. The capability
is additive and versioned: existing deployments that don't configure it, and
relays that don't implement the verb, are entirely unaffected.

## Configuration (no platform literals in the code)

All platform specifics come from env / vault, so the code stays provider-agnostic:

| env | meaning |
| --- | --- |
| `ROOM_READ_BACKEND` | force `generic` or `appservice` (else auto) |
| `RELAY_URL` / `REMOTE_TASK_URL` | relay base for the generic verb |
| `RELAY_TOKEN` / `REMOTE_TASK_TOKEN` | bearer for the relay (optional) |
| `HOMESERVER` / `HOMESERVER_URL` | homeserver base for the appservice backend |
| `AS_TOKEN` / `APPSERVICE_TOKEN` | AppService token (store in vault; never commit) |
| `AGENT_MXID` | the agent identity to masquerade as / read for |
| `ROOM_READ_GATE` | path to the gate JSON (default `<workspace>/state/room-read-gate.json`) |

The AppService token is a privileged secret — keep it in the vault
(`vault set AS_TOKEN ...`) and inject at runtime; it must never land in the repo
or a task file.

## Tests

`python3 skills/appservice-room-read/test_room_read.py` — 19 unit tests covering
the gate (default-deny + opt-in paths), backend selection, normalisation, and
graceful degrade (incl. 404/403 → no-op). No network.

## Status

- Tool + gate + dual backend + tests: done (this skill).
- Live end-to-end verification against the homeserver: needs `AS_TOKEN` (the
  privileged AppService secret) in the vault — pending.
- Relay-side `GET /v1/rooms/{room}/messages` implementation for the generic
  backend: the paired half (relay/box), tracked separately.
