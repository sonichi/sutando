# Connections: one install, several homeservers

A **connection** is one gateway this install's agent is enrolled at, served by one
remote-gateway bridge instance. The directory of connections lives at
`<workspace>/state/connections.json`, and `src/connections.py` is its only writer:
the CLI (`scripts/connections.py`), the runtime API (`connection.*` methods) and
startup (`src/startup-runtime.sh`) all call that module and never touch the file.

## The model

| Field | Meaning |
|---|---|
| `server` | The homeserver the connection belongs to (`s.space`), never the gateway hostname (`chat.s.space` and `matrix.s.space` are endpoints of that server). |
| `instance` | The bridge instance slug (`s-space`): `GATEWAY_INSTANCE`, the suffix on `state/gateway-status.<instance>.json`, the `task-<instance>~<broker-id>` id prefix, the `channels/<instance>-ag2space/` config dir. `""` only for the legacy unsuffixed bridge imported from `REMOTE_TASK_TOKEN`. |
| `gateway_url` | Stored exactly as the relay issued it inside the `url|secret` token. Never rewritten: an endpoint rename never touches stored rows. |
| `token_ref` | The vault key the secret lives under (`AG2_REMOTE_TOKEN_<INST>`; `REMOTE_TASK_TOKEN` for the legacy row). The secret itself is never in this file. |
| `primary` | Exactly one row. Proactive nudges and the owner-DM defaults stay with the primary; a secondary bridge launches with no `REMOTE_PROACTIVE_ROOM`. |
| `added_by` | `login`, `owner`, `ui` or `env`. |
| `added_at` | ISO 8601 UTC. |
| `verified` | Whether the server came from the gateway's own answer (below) rather than from the gateway hostname. |
| `agent_mxid` | The agent's identity at that server, when the gateway named it. |

The record is validated on every read (schema, bounds, one primary, unique instance /
token_ref / server) and published atomically under a lock file beside it, so a reader
never sees a half-written or forged row.

### Two policies, kept apart

The connections list is the install's **instance directory**: it decides which
bridges startup launches and where each one's state lives. It is *not* an
authorization list. Adding a connection never widens the Sutando Cloud bearer
allowlist, never reads or writes `state/auth/` or anything `cloud_auth` reads, and
grants the new gateway nothing beyond what its own token already carries.
`tests/connections-no-cloud-trust.test.py` pins that.

## Adding: preview, then confirm

Adding is two-step and does not care where the code came from (pasted into the UI,
typed at the CLI, handed over by a browser flow, present in the environment):

1. **Preview.** `preview_connection(token)` performs the bridge's own authenticated
   probe (`GET /v1/agents` under the token's bearer) against the gateway embedded in
   the token. When the gateway names exactly one agent, the server is that mxid's
   server and the preview is `verified`. Otherwise the server falls back to the
   gateway hostname with `verified: false`, so the UI can say so. Nothing is saved.
2. **Confirm.** `add_connection(token, expected_server=<what the preview showed>)`
   runs the preview again and saves only if the identity still matches; a changed
   answer is a `ConnectionMismatch` (the runtime API answers `CONFLICT`, the CLI exits
   1). The secret goes to the vault under `token_ref` first; if the vault refuses, the
   add fails closed and nothing is written.

Adding is **idempotent per server**: a server that already has a row returns that
row unchanged and starts no second bridge (the bridge's singleton lock is the backstop,
not the policy). Adding **never changes which row is primary** except that the very
first row saved becomes primary. `set_primary(instance)` is the one way to move it,
and moving it is what moves proactive and owner-DM routing at the next launch.

## State

`status_for(row)` joins the row with the bridge's own
`state/gateway-status[.<instance>].json` and with the launch record `launch` leaves
under `state/connections-launch/<instance>.json`:

| `state` | When |
|---|---|
| `pending` | Saved; no sidecar yet (bridge not up, or never launched). |
| `connected` | The sidecar says `connected: true`. |
| `disconnected` | The sidecar says `connected: false` for a non-auth reason. |
| `failed` | With `reason`: `no_token`, `launch_failed`, `auth_rejected` (the sidecar reports a 401/403 or a pending re-enrol). |

`reconnect(instance)` stops the one bridge holding that instance's singleton lock and
launches it again; the other connections keep running. `remove_connection(instance)`
drops the row, its vault key and its launch record, and nothing else; the primary needs
`force`, and the primary flag then passes to the first remaining row.

## Startup

`start_gateway_lanes` (in `src/startup-runtime.sh`) first runs `import-env`, which
creates a row once for `REMOTE_TASK_TOKEN` and each `AG2_REMOTE_TOKEN_<INST>` in the
environment (copying the secret into the vault unless one is already there; never
overwriting a row), then launches one bridge per `launch-plan` line through
`scripts/connections.py launch <instance>`. The plan lists every row with an instance
plus any env token the import could not record, so an unavailable vault degrades to
the previous behaviour instead of losing a gateway. The legacy unsuffixed bridge keeps
its own launch path.

The bridge environment (`GATEWAY_INSTANCE`, `REMOTE_TASK_TOKEN` from the env var of
the same name or the vault, empty `REMOTE_TASK_URL`, `REMOTE_TASK_CHANNEL_DIR`,
`REMOTE_PROACTIVE_ROOM` only for the primary, `SUTANDO_SUPERVISED=1`) is composed in
`connections.launch_env`, nowhere else.

## Surfaces

- CLI: `scripts/connections.py list | add | set-primary | remove | reconnect | launch | import-env | launch-plan`.
- Runtime API: `connection.list`, `connection.add` (`{token}` previews; `{token, confirm: true, expected_server}` saves), `connection.remove`, `connection.set_primary`, `connection.reconnect`. These are the daemon-native form of the HTTP resource (GET / POST / DELETE / POST `<instance>/primary` / POST `<instance>/reconnect`).

Until the homeserver-family resolver lands, an unverified row's server is the gateway
hostname; the resolver will then derive the server family for those rows.
