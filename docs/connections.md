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
| `primary` | At most one row. Proactive nudges and the owner-DM defaults stay with the primary; every other bridge launches with `REMOTE_PROACTIVE_ROOM` set empty, whatever the parent environment holds. |
| `added_by` | `login`, `owner`, `ui` or `env`. |
| `added_at` | ISO 8601 UTC. |
| `verified` | Whether the server came from the gateway's own answer (below) rather than from the gateway hostname. |
| `server_source` | Where `server` came from: `identity` (the DotSpace the resolver registers for the gateway host, confirmed by the gateway's own agent listing), `resolver` (the registered DotSpace alone), or `gateway` (the host itself, provisional). |
| `agent_mxid` | The agent's identity at that server, when the gateway named it. |
| `agent_display_name`, `agent_avatar_url`, `owner_mxid` | Optional, from the same listing entry as `agent_mxid` and only on a verified preview: the entry's `display_name`, its `avatar_url` (stored only as an `mxc://` URI) and its `owner` mxid. Absent until the gateway sends them; never a secret. The list spells every one of them out, `null` when unknown. |
| `disabled` | `true` after `disconnect`: the bridge is stopped and the launch plan skips the row, token and launch record kept, until `reconnect` or `update_token` clears it. Written only over a stopped bridge (below). Absent on older rows, which load as enabled. |
| `parked` | `true` after `promote` moved the primary off this row: its bridge is stopped and nothing launches it, the legacy row included (its launchers skip it and the bridge stands down on it), while the row, its vault key and its launch record stay. Written only over a stopped bridge. `unpark`, or a `promote` back, clears it. Absent means not parked. |
| `launch_keys` | Optional: `AG2_REMOTE_TOKEN_<INST>` names a host wrote for this same server after the row existed (`import-env` records them instead of adding a second row). A host's launcher runs such a key as `<inst>` lowercased, so those are the row's alias instances: the plan never launches them, `start`/`launch` report a bridge already up under one (`lease: alias_running`) instead of spawning beside it, `running` counts it, and `disconnect`, `remove`, `reconnect` and `update_token` stop it with the row's own bridge. |

An explicit `instance` must be a slug of `[A-Za-z0-9_-]{1,32}` and not one of the HTTP
resource's fixed names (`known-servers`, `token`, `legacy`): a row so named could never be addressed
there, so `add` refuses it (400, CLI exit 1) and nothing is written.

Beside the rows, the record may carry `legacy_parked: true`: `promote` stopped a legacy
unsuffixed bridge that has no row of its own (its `REMOTE_TASK_TOKEN` names a server a named row
already covers, so `import-env` gave it none). `legacy_parked` reads it like a parked legacy row,
and `unpark ''` clears it.

### Refusals by code

Every refusal that comes from a row's or a code's state is a `connections.Refusal` whose
`CODE` each surface returns as is: HTTP **409** `{"error": "<code>"}`, runtime API
**CONFLICT** with the module's message, CLI exit 1 with `connections: <code>: ...` on
stderr. Nothing is written when one is raised, except `token_restore_failed` and `token_state_unknown`,
which say what reading the vault back found.

| Code | Raised by | Meaning |
|---|---|---|
| `primary_cannot_disconnect` | `disconnect` | The row is primary; make another row primary first. |
| `disabled_cannot_be_primary` | `set_primary`, `promote` | The row is disconnected; a disabled primary never launches, so no bridge would carry the proactive room. Reconnect it first. |
| `parked_cannot_be_primary` | `set_primary` | The row is parked and would not launch. `promote` it (which un-parks it and starts its bridge), or `unpark` it first. |
| `legacy_bridge_not_running` | `promote` | The target is the legacy row and its bridge is not running; the engine never launches that bridge, so `unpark` it, start it (`bash src/startup.sh`, or let the launchd job resume it), then promote. Nothing changed. |
| `new_primary_not_started` | `promote` | The new row's bridge did not start (the message names the launch `reason`). The old primary was never stopped; the primary flag and a `parked` flag the call cleared are restored. |
| `promote_conflict` | `promote` | Another writer moved the primary, or removed or parked the row this promote was to park, while the new bridge was connecting. That change stands: the bridge this call started is stopped, a `parked` flag it cleared is restored, and nothing was parked or stopped. Re-read the list and retry if still wanted. |
| `new_primary_not_connected` | `promote` | The new row's bridge started but its own status never showed a gateway poll within `CONNECT_TIMEOUT_S` (`reason: timeout`), showed its token rejected (`auth_rejected`), or the bridge exited (`exited`). Rolled back: that bridge is stopped, the primary and `parked` flags are restored, a secondary bridge the call stopped to relaunch is started again, and the old primary's bridge was never touched. |
| `legacy_row_unmanaged` | `disconnect`, `update_token` | The legacy unsuffixed row (`instance: ""`) is launched from `REMOTE_TASK_TOKEN` in the environment, not from this directory; change that variable instead. |
| `bridge_still_running` | `disconnect`, `remove`, `reconnect`, `update_token`, `promote` | The bridge still held its lease past the stop deadline, released it but was still running at the deadline, or refused the signal; the message names the pid. Nothing changed: the row stays (never marked `disabled` over a live process), `remove` keeps the row, launch record, status file and vault key, `reconnect` spawns no second bridge, and `update_token` writes nothing and says the bridge was not confirmed stopped and may still be running on the old code. `promote` rolls back: the old primary stays primary and unparked, a bridge the call started for the new row is stopped and a `parked` flag it cleared is restored (left cleared when that bridge will not stop either). Retrying is safe. |
| `bridge_lease_indeterminate` | `disconnect`, `remove`, `reconnect`, `update_token`, `set_primary`, `promote` | The bridge's lease file exists but does not prove which process holds it (unreadable or malformed, written for another role, workspace or schema such as a copied lease, another host's, no recorded start time, or a start time that cannot be checked); the message names which. Nothing is signalled or written. Restart that bridge (`bash src/restart.sh`) so it writes a current lease. |
| `legacy_primary_running` | `set_primary` | The legacy unsuffixed bridge holds its lease, whatever the record's primary (legacy, none, or a named row). It settles the proactive room once, right after taking its lease, so it may carry it and would keep it. Stop it (`bash src/restart.sh --stop-only`), make the new row primary, then start again (`bash src/startup.sh`). |
| `token_source_shadowed` | `update_token` | The row's `token_ref` (`AG2_REMOTE_TOKEN_<INST>`) is set in the environment, which the bridge reads before the vault; the new secret would be stored and never used. Unset the variable (or let `import-env` own that row) and retry. |
| `token_server_mismatch` | `update_token` | The code previews as another server, or as a server another row already holds. |
| `token_agent_mismatch` | `update_token` | The row names an agent and the code's gateway names another; that agent is its own connection. |
| `token_unverified` | `update_token` | The row names an agent and the preview did not confirm it (gateway unreachable, an error, a listing naming none or several); the code is not shown to be this connection's. |
| `token_rejected` | `add`, `update_token` | The gateway answered 401/403 on the preview. |
| `vault_unreadable` | `update_token` | The vault could not say whether it holds the row's current secret (a read error, not a miss). Refused before the stop and the write, because a rollback to "absent" would delete a working code. |
| `token_restore_failed` | `update_token` | Not a clean refusal: a write after the vault write began failed, and so did putting the previous secret back. The message names both failures; read back, the vault holds the **new** code while the row is unchanged. The bridge is not relaunched. |
| `token_state_unknown` | `update_token` | Not a clean refusal: a write after the vault write began failed and reading the key back could not establish which secret it holds (the read failed, or it showed another writer's value, which is left in place). The message claims neither code and carries no secret; the bridge is not relaunched on a guess. Run `update-token` again with the code you want. |
| `vault_key_in_use` | `add` | The vault already holds a secret under the new row's key and no row owns it. |

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

1. **Preview.** `preview_connection(token)` first checks the gateway URL and host: the
   URL must be `https`, and the host a multi-label server name under none of the reserved
   suffixes (`.localhost .local .internal .test .example .invalid .onion .home.arpa`),
   never an IP literal or a single label (the resolver's own `is_server_name` and
   never-fetched rules). It then resolves the name **once** and refuses it if any answer
   is loopback, private, link-local, multicast, reserved, unspecified or an IPv4-mapped
   form of those. Any of these is `invalid_gateway` before a single packet, so a token
   cannot aim the probe at the operator's own machine or network; a name that does not
   resolve is `unreachable (resolution)`, also unprobed. The probe itself is **pinned**
   to the vetted address (TCP to that IP, SNI and `Host` carrying the original name), so
   a DNS re-answer between the check and the connect cannot swap the target. It then
   asks the DotSpace resolver which server the host is registered for
   (`dotspace_for(gateway_host)`, source config or well-known) and runs the bridge's own
   authenticated probe (`GET /v1/agents` under the token's bearer, following no redirect).
   The same guard runs on confirm (which previews again server-side) and on `reconnect`'s
   identity learn. **A listing can confirm an identity, never establish one:**
   `verified: true` (`server_source: identity`) only when the gateway names exactly one
   agent whose server is the registered server. A registered host whose listing disagrees
   or is missing stays `resolver`; an unregistered host is the server itself (`gateway`)
   whatever the listing claims. `error` names a refusal: `auth_rejected` (401/403),
   `not_a_gateway` (404), `http_<code>`, or `unreachable (...)`. Nothing is saved.
2. **Confirm.** `add_connection(token, expected_server=<what the preview showed>)`
   runs the preview again and saves only if the identity still matches; a changed
   answer is a `ConnectionMismatch` (the runtime API answers `CONFLICT`, HTTP 409
   `mismatch`, the CLI exits 1). A token the gateway rejected on the probe is never
   stored (`TokenRejected`, 409 `token_rejected`); an unreachable gateway is stored as
   a pending, unverified row. The secret goes to the vault under `token_ref` first; a
   vault key that already holds a secret no row owns is never overwritten
   (`VaultKeyInUse`, 409 `vault_key_in_use`), and if the vault refuses, the add fails
   closed and nothing is written.

Adding through the HTTP route or the runtime API **starts the row's bridge at once**
(`start_added`, which calls `start`: `launch` unless a process already holds the lease), so a
saved connection never waits for the next restart; the response carries that `launch`
record, and a start that fails leaves the row `failed` with its `reason`. A disabled row is
not started (`reconnect` is the verb), nor the legacy row. Before saving, an add imports any
`AG2_REMOTE_TOKEN_<INST>` launch key (environment or AG2 Space channel env file) that no row holds
yet, under the instance its own launcher runs it as; a token for one of those servers adopts that row
(`created: false`) rather than saving a second row whose bridge would hold another lease. `start`'s
check for a running bridge is advisory; the bridge lease is what keeps one holder. The CLI `add` saves only; `start`
runs the same unit. Adding is **idempotent per connection** and starts no second bridge (the
bridge's singleton lock is the backstop, not the policy). A row whose server the registry or an
identity vouches for (`resolver`/`identity`) matches a token for the same server, so one
registered homeserver's endpoints (`chat.` and `matrix.`) are one connection. A
provisional (`gateway`) row matches only a token for its own gateway host, so it can
never stand in for a real connection elsewhere: a gateway at another host claiming
someone else's identity gets its own provisional row and the real token is still stored.
A verified preview, or a `reconnect` that learns the identity, **re-keys** a provisional
row to the identity's server in place: one row, one bridge, the instance slug and vault
key unchanged. A row keyed by identity is never demoted.

A saved row is primary only when the install has no primary yet. `import-env` gives the
flag only to the `REMOTE_TASK_TOKEN` row (the legacy unsuffixed bridge), never to a
named `AG2_REMOTE_TOKEN_<INST>`; removing the primary leaves none until `set_primary`
or the next saved row. Adding **never changes which row is primary** except that the very
first row saved becomes primary. `set_primary(instance)` is the one way to move it,
and moving it is what moves proactive and owner-DM routing **at the next launch**: a bridge
already running keeps the environment it was launched with. A disabled row is refused
(`disabled_cannot_be_primary`). The returned row carries `relaunch_needed: true` when the
new primary's bridge is running (it has no proactive room yet); the panel shows it. The
handoff order is therefore **promote B, reconnect B, then disconnect A**: after the
reconnect B's launch carries `REMOTE_PROACTIVE_ROOM`, and A's next launch does not. Promoting
and disconnecting A straight away would stop the only bridge that had the room.

Unaddressed proactive messages (a `results/proactive-*.txt` naming no room) and the owner's
cards go to the primary row's bridge, under its instance or a launch-key alias, read from the
record on every pass (`connections.owns_unaddressed`, injected as the bridge's
`OWNS_UNADDRESSED`), so a promotion moves them without a relaunch. With no named primary, or an
unreadable record, the legacy unsuffixed bridge owns them. While the primary's bridge is down they
are held, never handed to another lane. A desktop host that parks the primary's lane because the
owner signed in on another homeserver moves the primary to the signed-in lane.

While the **legacy** bridge (`instance: ""`) runs the order differs. Its launchers (the
launchd wrapper and the bare launch in `startup-runtime.sh`) read `primary` before they spawn
it, and the bridge itself settles the room once, right after it takes its lease: it re-reads
`primary` under the record lock (`connections.legacy_keeps_proactive_room`, injected as the
bridge's `PROACTIVE_ROOM_KEEP`) and drops `REMOTE_PROACTIVE_ROOM` when a named row is primary.
A running legacy bridge never learns about a later promotion, so `set_primary` of any named row
is refused while the legacy bridge holds its lease (`legacy_primary_running`), whatever the
record's primary, before anything is written. Both sides take the record lock, and the bridge
takes its lease before it reads: either the promotion committed first and the bridge drops the
room, or the bridge's lease is already there and the promotion is refused; exactly one bridge
carries the room. An unreadable record keeps the room (the same fail-open as the env-only launch). The handoff is **stop the legacy bridge** (`bash src/restart.sh
--stop-only`), **promote B**, **start again** (`bash src/startup.sh`): the legacy bridge comes
up with `REMOTE_PROACTIVE_ROOM` empty and B's launch carries it.

### Promote: one operation, old primary running

`promote(instance)` is the move a running install makes when the owner adds a homeserver and
wants it primary now, with the old primary's bridge (often the legacy one, on a terminal-run core)
still up. In order:

1. Under the record lock: refuse a disabled row (`disabled_cannot_be_primary`); note the current
   owner of unaddressed messages (the primary row, else the legacy row while its bridge runs);
   clear the row's `parked` flag and make it the only primary, so `launch_env` hands its bridge
   `REMOTE_PROACTIVE_ROOM`.
2. If the row's bridge already runs, it was launched as a secondary without the room: stop it.
   Then `start` it. A failure is `new_primary_not_started`.
3. Wait up to `CONNECT_TIMEOUT_S` (60 s, past the bridge's first long poll) for that bridge's own
   `gateway-status.<instance>.json`, written by its pid, to show `connected: true`: the gateway
   accepted its token. A rejected token, an exit or the deadline is `new_primary_not_connected`.
   Steps 2 and 3 roll back on failure and the old primary's bridge is never touched.
4. Under one hold of the record lock: stop the old owner's bridge by the same path `disconnect`
   uses (lease-proven pid, SIGTERM, wait for lease release and exit) and mark that row `parked`;
   a legacy bridge with no row of its own is stopped and recorded as `legacy_parked`. First it
   checks the row is still the primary and the row to park is still there and unparked; if another
   writer changed either during step 3, it is `promote_conflict`. A refused stop or a conflict
   rolls back as the table says.

`promote('')`, back to the legacy row, needs that bridge running (`legacy_bridge_not_running`:
the engine never launches it) and runs step 4 alone. The legacy bridge settled the proactive room
when it started, so when a named row was primary it runs without the room: the answer carries
`relaunch_needed: true`, and restarting it (`bash src/restart.sh`) brings it up with the room.

Ownership never doubles: delivery is claim-based and every bridge reads `owns_unaddressed` from the
record on each pass, so before the flip only the old bridge owns unaddressed messages and cards,
after it only the new one, and until the new bridge polls nobody does (they wait, never lost).
During the connect wait (step 3, `CONNECT_TIMEOUT_S`, 60 s at most) both bridges hold
`REMOTE_PROACTIVE_ROOM` in their environment; only the one `owns_unaddressed` names posts there. The
result is the new row plus `previous` (the parked instance, `""` for the legacy row, `null` when
nothing was parked), `stopped_pid`, `legacy_stopped_pid` (a rowless legacy bridge stopped, else 0),
`launch` (the start record, `null` for the legacy row), `changed` and `relaunch_needed` (the new
primary's bridge runs without the proactive room and needs a restart).
Promoting the current primary is a no-op that only starts its bridge if it is down
(`changed: false`), so a retry after any failure is safe. A promote back to a parked row un-parks
it and parks whichever row it takes the primary from.

A parked **legacy** row needs no change to `REMOTE_TASK_TOKEN`: the engine owns the flag. The
launchd wrapper checks `connections.py legacy-parked` before each launch and, while parked,
launches nothing (no owner alert) but keeps the agent-bridge supervisor running, and resumes by
itself once the row is unparked. The bare launch in `startup-runtime.sh` skips it with a ⊘ line.
The bridge itself asks `connections.legacy_parked` under the record lock right after taking its
lease (injected as the bridge's `STAND_DOWN_GATE`) and exits 75 when parked, so a launch that
raced the promote never polls. An unreadable record parks nothing.

`unpark(instance)` clears the flag. A named row's bridge is started as a secondary (`launch` in the
answer); the legacy row's launchers resume it (the launchd job within its relaunch delay, a bare
install at the next `bash src/startup.sh`). Un-parking never moves the primary and is idempotent.
`reconnect` also clears `parked`.

## State

`status_for(row)` joins the row with the bridge's own
`state/gateway-status[.<instance>].json` and with the launch record `launch` leaves
under `state/connections-launch/<instance>.json`:

| `state` | When |
|---|---|
| `pending` | Saved; no sidecar yet (bridge not up, or never launched). |
| `connected` | The sidecar says `connected: true`. |
| `disconnected` | The sidecar says `connected: false` for a non-auth reason. |
| `parked` | With `reason: disabled`: the owner disconnected the row; its bridge is stopped and no launcher starts it until `reconnect` (whatever a stale sidecar still says). With `reason: parked`: `promote` moved the primary off it; down until `unpark` or a promote back. |
| `failed` | With `reason`: `no_token`, `launch_failed`, `lease_timeout`, `exited` (the child ended and no other process holds the lease; `rc` in the launch record), `retained_lease`, `auth_rejected` (the sidecar reports a 401/403 or a pending re-enrol). |

Beside `state`, every row carries `running`: `true` while a process is proven to hold the
row's bridge lease, `false` when none does, `null` when the lease proves nothing
(`bridge_lease_indeterminate`).

Which process runs a bridge is read from the bridge's own lease, `state/locks/gateway-bridge.<instance>.lock`, the JSON record `workspace_lock` writes and heartbeats: `bridge_pid` reads it with `workspace_lock.read_lease` and accepts a pid only when `workspace_lock.lease_process` proves it is the process that wrote the record (this host, same recorded start time), so no unproven pid is ever signalled and a lease that proves nothing is `bridge_lease_indeterminate`, never "no bridge". `read_lease` also binds the record to what was asked: its `role`, `workspace` (compared as real paths) and `schema_version` must be the caller's, so a record copied from another bridge's path never lends that bridge's pid to this one. The writer is generation-aware the same way: acquire, heartbeat, release and retain take a record as their own only when pid, host **and** start time match, and a same-host holder whose pid now runs with another start time is reaped as gone.

The start time is `/proc` on Linux, `sysctl` on macOS and, on Windows, the process creation time from `outbox.process_identity` (the repository's one Windows process-identity primitive), read once per decision and used by the writer and the reader alike (an `UNKNOWN` answer is
indeterminate, never a generation mismatch that reaps a live holder), so disconnect, remove, reconnect and update-token work there too. Freshness is wall-clock seconds in the record: a macOS clock step forward of more than the stale window (90 s) can make a live bridge's lease read as stale, so a successor reaps it; the next heartbeat then fails and the old bridge stops polling.

`disconnect(instance)` stops the one bridge holding that instance's lease (the
same stop `remove_connection` and `reconnect` use) and **then** marks the row `disabled`; the
row, its vault key and its launch record stay, so the owner can come back without a new
token. The stop is SIGTERM, then a wait of `STOP_TIMEOUT_S` until no process holds the lease
(released, or its writer gone) **and** the signalled process has exited, so a status write it
makes after releasing its lease lands before the stop returns; a bridge that still holds it, is
still running past the deadline after releasing it, or refuses the signal is
`bridge_still_running` with its pid, and the row is not touched, so `disabled` never masks a
live sidecar. The launch plan omits a
disabled row, so a core restart leaves it down. The primary cannot be disconnected while it
is primary (`primary_cannot_disconnect`): make another row primary first. The legacy
unsuffixed row cannot be disconnected here at all (`legacy_row_unmanaged`): the legacy
launchers start it from `REMOTE_TASK_TOKEN` and never read the flag. Disconnecting a
disabled row is a no-op. `launch` holds the record lock from the plan read until its child
is proven to hold the lease, by the same check the stop path uses (bound role and workspace,
`lease_process` returning the child's pid, fresh), or has exited, so a disconnect or remove
that starts in between waits and then stops the bridge it started; a bridge never comes up
against a row that already says it is down. A same-pid record with another start time or
from another host is never taken for the child's lease, and the child is polled around the proof,
so one that wrote its lease and then exited (still unreaped) is never held. A child that **exits** is a success
only when another process is proven to hold the lease (`lease: exited`, `holder_pid`: it stood
down to a live duplicate). A child that stood down (rc 75) to a **retained** lease whose writer
has gone is spawned once more when that lease goes stale within the bound, else the launch
fails with `retained_lease`; any other exit fails with `exited` and the child's rc. A child
still without a lease after `LEASE_TIMEOUT_S` is killed and the launch fails (`lease_timeout`).
`reconnect(instance)` clears the flag, stops the bridge if one is
running, learns the identity its token answers to now and launches it again; the other
connections keep running. A plain `launch` of a disabled row is refused by name
("disconnected; reconnect it"). `update_token(instance, token)` is for a code that was
rotated or revoked on the homeserver (Get new token, Revoke) or an agent moved away and
back. It is refused with the vault and the row untouched, in this order: on the legacy row
(`legacy_row_unmanaged`); when the row's `token_ref` is **set in the environment**
(`token_source_shadowed`) — `launch_env` and the bridge's own source order read the variable
before the vault, so a vault write would report success and the relaunch, and every later
restart, would run on the old code; rows that `import-env` created from
`AG2_REMOTE_TOKEN_<INST>` are the usual case, and the owner unsets the variable first (or
lets the import path own that row); for a code the gateway rejected (`token_rejected`); for
another server, or for a server another row already holds (`token_server_mismatch`); and,
while the row names an agent, unless the preview **confirms that same agent**: an unconfirmed
preview (gateway unreachable, an error, a listing naming none or several agents) is
`token_unverified`, another agent `token_agent_mismatch` (a code for another agent is a new
connection, never pasted over this row). A provisional row with no agent keeps its earlier
behaviour and learns the identity. The new row is built and validated **before** any
external effect; only then is the bridge stopped (`bridge_still_running` if it will not) and
the vault written. The row, its `token_ref` and the previous secret are read **inside the
record transaction**, right before the stop and the vault write, with a read that tells a miss
from a failure (`vault_unreadable` on a failure), so a concurrent update that committed, or a
remove and re-add, is never undone by a stale snapshot. Once the vault write has begun, any
failure (the Keychain write landed but the vault manifest did not, or the record write failed)
is reconciled by **reading the key back** while the same hold of the record lock is kept (a writer
that would commit meanwhile waits), compared by digest and never logged.

The vault has **one writer transaction**, `vault_intercept.writer_lock`: a cross-process lock on
`writer.lock` beside the canonical manifest (`state/secret-vault/`), re-entrant within the thread
that holds it. Every vault write runs inside it, Keychain and manifest alike: `set_vault_key`,
`delete_vault_key`, chat interception (`vault set` in a message) and the secret-vault CLI all reach
the same locked writer. Before it stops the bridge, `update_token` probes the lock, waiting at
most 2 s so a chat interception's brief write is absorbed: a lock still held after that refuses the
update with `vault_busy` and the bridge is never stopped. The probe is released before the stop, because a stopping bridge may be finishing its own
vault write. After the stop it takes the lock again (bounded, below) and holds it from the
prior-value snapshot through the new vault write, the record publication and any restore, so no
other vault writer, in this process or another, can land in between. A narrow race remains: a
writer that takes the lock between the probe and the re-take makes the re-take wait for it, with
the bridge already stopped (and relaunched on the unchanged code if that wait runs out). The lock order is
`connections.lock`, then the vault lock; nothing in the vault layer takes `connections.lock`. The
lock is released when its holder raises or exits (a killed holder's lock is dropped by the
kernel), and its file stays empty. A lock that cannot be taken fails the write.
The tradeoff is that vault writes are serialized: every other vault writer waits for the length
of an `update_token` transaction (about 7 s in the live receipt). Both waits are bounded, so a
hung Keychain prompt cannot block every vault writer on the host: a writer that cannot take the
lock within `_WRITER_LOCK_WAIT_S` fails with `vault_busy` and writes nothing, and a `security`
store or delete that gives no answer within 15 s is killed and the key is read back (5 s), so
the manifest follows what the Keychain holds: a write that took effect anyway is kept (a store
registered, a delete dropped) and succeeds; one that did not fails with `vault_keychain_timeout`,
nothing changed; a read-back that also fails is `vault_keychain_state_unknown`, the manifest
unchanged (a delete's entry is kept, so no secret is stranded; an `add` that saw it also deletes
the key it may have stored). The lock is released either way. `_WRITER_LOCK_WAIT_S` (65 s) is
derived from those bounds to exceed the longest legitimate hold, an `update_token` that stores,
fails and restores: two writes each with a read-back plus three reads (55 s), plus 10 s margin.
The bridge stop is outside the lock, so it does not count. Through `connections` both surface as `vault_unavailable` (HTTP 503), the
message naming the code; only a leading `vault_*` code is passed through from the vault's error,
never its free text, so no Keychain output or secret reaches the message.

The lock lives beside the manifest of the **configured** workspace (`resolve_workspace`), not
the `workspace=` a `connections` call is given: the record and the vault lock can sit in different
workspaces. Keychain items are per host and per key, not per workspace, so two checkouts on one
host with different workspaces share the same `AG2_REMOTE_TOKEN_<INST>` items under two different
locks, and their writes to the same key are not serialized against each other. Give each
checkout's rows distinct instance names, or point both at one workspace.

The restore is still a compare-and-swap on the stored value: the previous secret is written back
only when a read shows exactly the new code this update wrote, then read back again. Read back
as the previous secret, a bridge stopped for the update is relaunched on it and the error says
whether that worked; still the new code, `token_restore_failed`; any other value or a read that
fails, `token_state_unknown`, with the row unchanged. Neither of the last two relaunches. A public
write that arrives meanwhile, a different value or the same new code, waits for the transaction
and then lands, so its value is the final one. Only a write that bypasses `vault_intercept` (the
`security` command run by hand) is outside the transaction.

On success `update_token` replaces the secret under the row's existing
`token_ref`, keeps the instance, primary flag, `added_at` and launch record, updates
`gateway_url` and the identity fields from the preview, clears `disabled` and relaunches the
bridge the way `reconnect` does, so a disconnected or failed row comes back on the new code;
a relaunch that fails is the row's `failed` state, not an error of the update. A row keyed by
identity refreshes its identity fields only from a verified preview of **the same agent**:
adding or previewing another agent's code on that server leaves the row (and its token)
exactly as it was. `remove_connection(instance)`
stops that bridge the same way, then drops the row, its vault key, its launch record and its
status sidecar (`gateway-status.<instance>.json`, so a re-added name lists as `pending` until
its new bridge reports, not with the removed row's `connected` and `last_ok_ts`), and nothing
else. All of it happens under the record lock, after the stop has seen the old bridge exit, so
neither a concurrent re-add of the same name nor the old bridge's last status write can be
undone or land afterwards. The legacy row's sidecar stays, since its bridge is not stopped here. The primary needs `force`, and no remaining row inherits the flag. A
removed env-imported row is remembered in the record's `removed_env` list, so
`import-env` does not bring it back while the variable is still set (it says
"connection removed earlier; unset the variable"); an explicit add of that key clears
the tombstone.

## Startup

`start_gateway_lanes` (in `src/startup-runtime.sh`) first runs `import-env`, which
creates a row once for `REMOTE_TASK_TOKEN` and each `AG2_REMOTE_TOKEN_<INST>` in the
environment (copying the secret into the vault unless one is already there; never
overwriting a row), then launches one bridge per `launch-plan` line through
`scripts/connections.py launch <instance>`. When the environment holds no
`REMOTE_TASK_TOKEN`, the primary row comes from the AG2 Space channel env file instead
(the one `scripts/channel-env.sh` resolves; `REMOTE_TASK_TOKEN` or the legacy
`AG2_REMOTE_TOKEN`, environment first when both exist): a desktop install signs in after
the engine started, so the connect flow reaches the file but never this process's
environment. `list` runs that import once when the record has never been written (an initialized record
that is empty stays empty: its rows were removed on purpose), so the panel
shows the connection on its next refresh without an engine restart; an import that fails
is logged and the list still answers. A record that exists still adopts, on `list`, any
`AG2_REMOTE_TOKEN_<INST>` the channel env file or environment holds that no row owns and
`removed_env` does not name: the key a desktop sign-in on another homeserver writes. A key whose
instance another row already holds is skipped without a write. A row's sidecar reads as not
connected when a pid-liveness check proves its writer gone: the pid is free, or now names a
process with another start time (`proc_start`). A hung but live bridge still reads as connected. The plan lists every enabled row with an
instance (a disabled row stays down, and its env token is not re-planned either) plus any
env token the import could not record, so an unavailable vault degrades to the previous
behaviour instead of losing a gateway. The plan is startup's only source of what to launch,
so `disconnect` needs nothing from the shell. An unreadable record never reads
as "no secondaries": `launch-plan` exits non-zero naming the parse error, startup prints
a ⚠ line and launches each `AG2_REMOTE_TOKEN_<INST>` with `launch --env-only`, from the
variable alone. That env-only path is fail-open by design: an unreadable record cannot say a row
is disabled, so its token launches anyway rather than leaving every secondary down. The legacy
unsuffixed bridge keeps its own launch path. Each `launch` waits up to `LEASE_TIMEOUT_S` (20 s)
for its child's lease, one bridge after another, so startup can take N × 20 s when every one of
N bridges stalls before its lease.

### Upgrading

A legacy bridge started by an earlier version does not settle the proactive room after its
lease. Before the first `set_primary` to a named row, restart once (`bash src/restart.sh
--stop-only`, then `bash src/startup.sh`): `set_primary` refuses while that bridge runs anyway
(`legacy_primary_running`), and the restarted bridge settles the room itself.

The bridge environment (`GATEWAY_INSTANCE`, `REMOTE_TASK_TOKEN` from the env var of
the same name or the vault, empty `REMOTE_TASK_URL`, `REMOTE_TASK_CHANNEL_DIR`,
`REMOTE_PROACTIVE_ROOM` only for the primary, `SUTANDO_SUPERVISED=1`) is composed in
`connections.launch_env`, nowhere else. The legacy unsuffixed bridge keeps both of its launch
paths (the launchd wrapper, and the bare launch when no job is installed); each asks
`connections.py primary` before every launch, and once a named row holds the primary launches
with `REMOTE_PROACTIVE_ROOM` empty, so exactly one bridge posts nudges. Otherwise the variable
passes through untouched, unset included: the bridge reads the desktop's device env only when it
is unset, so an exported empty value would silence nudges on a plain install.

## Known servers

`known_servers()` is the Add picker's offer: every DotSpace the resolver registers
(`dotspace.registered_instances()`: the configured `core.dotspace.instances` list, else
the built-in default; `src/dotspace.py` is the one place those names live), one entry per
server in registry order, joined read-only with the rows:
`{"server", "connected": <a row exists>, "instance": <that row's slug or null>, "primary", "disabled"}`.
Nothing is fetched and no vault key name is in the answer. A server with no row is what the
picker offers; one with a row is already in the directory (and, when `disabled`, resumes
through reconnect rather than a new add). A row for an unregistered server is in the list,
not here.

## Surfaces

- CLI: `scripts/connections.py list | known-servers | add | set-primary | promote | unpark | legacy-parked | remove | disconnect | reconnect | update-token | launch | start | import-env | launch-plan | primary`. `promote INSTANCE --repo DIR --logs-dir DIR` and `unpark INSTANCE --repo DIR --logs-dir DIR` (`''` names the legacy row); `legacy-parked` exits 0 when the legacy row is parked, else 1. `update-token INSTANCE -` reads the new code from stdin, which keeps it out of the process list (`ps` shows every argument); a literal code in its place is for scripts. `set-primary` prints "reconnect it" when the new primary's bridge is running.
- Runtime API (Unix socket, JSON-RPC): `connection.list`, `connection.known_servers`, `connection.add` (`{token}` previews; `{token, confirm: true, expected_server}` saves and starts the bridge), `connection.remove`, `connection.set_primary`, `connection.promote` and `connection.unpark` (`{instance}`, `""` for the legacy row; promote answers `{connection, previous, stopped_pid, launch, changed, relaunch_needed}`, unpark `{connection, launch}`), `connection.disconnect`, `connection.reconnect`, `connection.update_token` (`{instance, token}`). Every refusal by code (the table above) is `CONFLICT`, the same class the HTTP 409 carries.
- HTTP (`src/agent-api.py` on `127.0.0.1:7843`, the web client's transport; routes dispatch to the same units):

  | Route | Body | Answer |
  |---|---|---|
  | `GET /api/v1/connections` | — | 200 `{"connections": [rows joined with status, state, server_source, disabled, parked, legacy, identity fields; no secrets]}` |
  | `GET /api/v1/connections/known-servers` | — | 200 `{"servers": [{"server", "connected", "instance", "primary", "disabled"}]}`; matched before any `<instance>` route (405 for another method, never a slug lookup) |
  | `POST /api/v1/connections` | `{"token", "confirm": false}` | 200 `{"preview": {...}}` |
  | `POST /api/v1/connections` | `{"token", "confirm": true, "expected_server", "added_by": "ui"}` | 201 `{"created": true, "launch": record, "connection": row}`, the bridge started (`launch` null for a disabled row); 200 `{"created": false, ...}` for an existing row, started when it was not running; 409 `{"error": "mismatch", "server_from_token", "expected_server"}`; 400 `{"error": ...}`; 503 `{"error": "vault_unavailable"}` |
  | `DELETE /api/v1/connections/<instance>[?force=1]` | — | 204; 409 `{"error": "primary"}`; 409 `bridge_still_running`; 404 |
  | `POST /api/v1/connections/<instance>/primary` | `{}` | 200 `{"connection": row}` (with `relaunch_needed`); 409 `{"error": "disabled_cannot_be_primary"}`, `legacy_primary_running` or `bridge_lease_indeterminate` (nothing moved); 404 |
  | `POST /api/v1/connections/<instance>/promote` | `{}` | 200 `{"connection": row, "previous", "stopped_pid", "launch", "changed", "relaunch_needed"}`: the new row's bridge relaunched as primary and connected, the old owner's stopped and its row `parked`; 409 `disabled_cannot_be_primary`, `legacy_bridge_not_running`, `new_primary_not_started`, `new_primary_not_connected`, `promote_conflict`, `bridge_still_running` or `bridge_lease_indeterminate` (rolled back, primary unmoved); 404 |
  | `POST /api/v1/connections/<instance>/unpark` | `{}` | 200 `{"connection": row, "launch": record}`: `parked` cleared and the bridge started as a secondary; 404 |
  | `POST /api/v1/connections/<instance>/disconnect` | `{}` | 200 `{"connection": row}` (`disabled: true`, state `parked`, bridge stopped and left down across restarts); 409 `{"error": "primary_cannot_disconnect"}` or `bridge_still_running` (row unchanged); 404 |
  | `POST /api/v1/connections/<instance>/reconnect` | `{}` | 202 `{"launch": record}`; clears `disabled` and starts the bridge now; 409 `bridge_still_running` |
  | `POST /api/v1/connections/<instance>/token` | `{"token"}` | 200 `{"connection": row}` (secret replaced under the same `token_ref`, `disabled` cleared, bridge relaunched); 409 `{"error": "<code>"}` for `token_source_shadowed`, `token_server_mismatch`, `token_agent_mismatch`, `token_unverified`, `token_rejected`, `bridge_still_running`, `vault_unreadable` (nothing changed), `token_restore_failed`, `token_state_unknown`; 400 without a token; 404 |

  The legacy row (`instance: ""`, `legacy: true` in the list) is addressed by the fixed segment
  `legacy`, matched before any slug: `POST /api/v1/connections/legacy/unpark`, `.../legacy/promote`,
  and the rest, which answer as for that row (`legacy_row_unmanaged` where the table says).

### Authorization for the HTTP routes

  Every connections route, GET included, is behind one gate (`src/local_api_gate.py`,
  the same module the dashboard's mutation gate lives in), checked in this order:

  1. **Host** must name loopback (`127.0.0.1`, `localhost`, `[::1]`) on the service's
     bound port — a DNS-rebinding page puts another name here — else 403.
  2. **Origin** must be absent (a local tool, a same-origin proxy) or loopback, else 403.
  3. **Bearer** `Authorization: Bearer <token>` must match, always: `SUTANDO_API_TOKEN`
     when the operator configured one, else the per-install secret the service mints
     once into `<workspace>/state/agent-api.token` (0600, atomic). No bearer is 401 on
     all six routes, with no `SUTANDO_API_TOKEN` set as much as with one.

  Responses carry `Access-Control-Allow-Origin` only for a loopback Origin whose request
  passed the whole gate (echoed, with `Vary: Origin`), never `*`, and never on a
  refusal; the preflight grants a loopback Origin `GET, POST, DELETE, OPTIONS` and
  `Content-Type, Authorization` and grants nothing else. `check_auth` (the `POST /task`
  gate, which is open when `SUTANDO_API_TOKEN` is unset) is **not** enough here: a
  reconnected bridge runs tasks at the local tier cap, which defaults to owner, so a
  page that could add or reconnect a connection would run owner-tier tasks under an
  attacker's token. The web client must present the bearer; the desktop shell reads
  `state/agent-api.token` and supplies it, and until that ships the UI shows "needs
  the desktop update" instead of calling these routes.

`instance_for` keeps a slug within the bridge's 32-character limit by appending a short
digest to a long server name, so two long names never share a slug.
