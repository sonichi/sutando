# room-ops — an agent's room-participation capability collection

**One skill, multiple tools.** Everything an agent does in a room beyond its task
inbox lives here as a tool, so the parity capabilities are self-evidently *one
collection* (not N scattered skills). Each tool is a thin **relay-only** client
verb sharing `_relay.py`; the relay/broker (box-side) owns the platform creds and
does the privileged Matrix ops + authoritative membership enforcement.

> Collection name `agent-room-ops` (provider-agnostic; alts: `room-participant`, `agent-chat-io`). Platform-tied names (e.g. `matrix-agent`) are avoided.
> `agent-room-ops`, `room-participant`). It is deliberately **provider-agnostic**,
> so platform-tied names (e.g. `matrix-agent`) are avoided.

## Tools

| tool | purpose | parity vs a chat bot-client |
| --- | --- | --- |
| `read <room>` | pull recent room history | discord `att.save`-context / channel read |
| `fetch <ref>` | inbound media → local path | discord inbound `att.save`→inbox |
| `send <room> <path>` | outbound file/image upload | discord outbound `[file:]` |
| `react <room> <event>` | add an `m.reaction` (ack) | discord `add_reaction` (👀/✅) |
| `unreact <room> <event>` | remove the agent's reaction | discord remove-on-reply |

```bash
python3 skills/agent-room-ops/room_ops.py read   '!room:hs' --limit 20 --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py fetch  'mxc://hs/abc' --room '!room:hs' --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py send   '!room:hs' /tmp/pic.png --caption 'fig 1' --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py react  '!room:hs' '$evt' --ack received --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py unreact '!room:hs' '$evt' --ack received --agent '@a:hs'
```

Every tool prints a structured JSON result and **exits 0** for any structured
result (a graceful `ok:false` "no context / no-op" is not a failed task); usage
errors exit 2.

## Shared design (every tool)

- **Orthogonal to the task file bridge** (`tasks/`→`results/`) — a separate
  synchronous call; the async loop is untouched.
- **Relay-only client.** Speaks only the `/v1` relay protocol; holds **no
  platform/AppService token**, never talks to a homeserver directly. Whether the
  relay backs a verb with a bot-client read or an AppService masquerade is the
  relay's (box-side) concern.
- **Membership enforced relay-side** (a non-member op → `403`). The optional
  per-agent client gate (`ROOM_OPS_GATE`, default-deny when present; absent →
  defer to the relay) is defense-in-depth, not the boundary.
- **Graceful degrade.** Missing relay / gate-deny / `404` (verb unimplemented) /
  `403` / network / oversize → structured `ok:false`, never raises. Additive +
  versioned: a relay without a verb just `404`s and the tool no-ops.
- **No platform literals** — relay coords from env/vault. Outbound media adds a
  path allowlist (`ROOM_MEDIA_ALLOW`) + 25 MiB size ceiling.

## Layout

```
agent-room-ops/
  _relay.py        shared: relay coords + per-agent gate + http + degrade
  read.py          read_room()
  media.py         fetch_media() / send_media()
  react.py         react() / unreact()
  room_ops.py      unified CLI dispatcher
  test_room_ops.py 29 tests, no network
```

## Configuration

| env | meaning |
| --- | --- |
| `RELAY_URL` / `REMOTE_TASK_URL` | relay base |
| `RELAY_TOKEN` / `REMOTE_TASK_TOKEN` | relay bearer (optional) |
| `AGENT_MXID` | the agent identity (relay resolves membership) |
| `ROOM_OPS_GATE` | optional client gate JSON (defense-in-depth) |
| `ROOM_MEDIA_INBOX` / `ROOM_MEDIA_ALLOW` | fetched-media dir / outbound path allowlist |

## Parity epic status

This collection is how an agent reaches **≥ a chat bot-client** (e.g.
`src/discord-bridge.py`) and surpasses it via Matrix. Per-tool slices:

| slice | tool(s) | status |
| --- | --- | --- |
| 1 room-read | `read` | merged (#1869), folded here |
| 2 media | `fetch` / `send` | folded here (was #1876) |
| 3 reactions | `react` / `unreact` | folded here (was #1877) |
| 4 delivery/routing markers | (`route`/marker tools) | next |
| — Matrix-surpass | custom events / edits / receipts / Spaces / widgets | upside |

Each slice's relay-side verb (membership-enforced) is the paired box-side half,
tracked in the parity epic.
