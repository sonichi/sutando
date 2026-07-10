# room-ops — an agent's room-participation capability collection

**One skill, multiple tools.** Everything an agent does in a room beyond its task
inbox lives here as a tool, so the parity capabilities are self-evidently *one
collection* (not N scattered skills). Each tool is a thin **gateway-only** client
verb sharing `_gateway.py`; the gateway/broker (box-side) owns the platform creds and
does the privileged Matrix ops + authoritative membership enforcement.

> Collection name `agent-room-ops` (provider-agnostic; alts: `room-participant`, `agent-chat-io`). Platform-tied names (e.g. `matrix-agent`) are avoided.

## Tools

| tool | purpose | parity vs a chat bot-client |
| --- | --- | --- |
| `read <room>` | pull recent room history | discord `att.save`-context / channel read |
| `fetch <ref>` | inbound media → local path | discord inbound `att.save`→inbox |
| `send <room> <path>` | outbound file/image upload | discord outbound `[file:]` |
| `react <room> <event>` | add an `m.reaction` (ack) | discord `add_reaction` (👀/✅) |
| `unreact <room> <event>` | remove the agent's reaction | discord remove-on-reply |
| `join <room>` | accept the agent's own pending invite | discord guild-join on invite |
| `doc get\|put\|rm <room>` | read/write/delete the room's shared vault docs (context, todo, memos — or any agent-defined folder) | the durable-state half: like a pinned channel wiki the bot can edit |

```bash
python3 skills/agent-room-ops/room_ops.py read   '!room:hs' --limit 20 --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py fetch  'mxc://hs/abc' --room '!room:hs' --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py send   '!room:hs' /tmp/pic.png --caption 'fig 1' --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py react  '!room:hs' '$evt' --ack received --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py unreact '!room:hs' '$evt' --ack received --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py join   '!room:hs' --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py doc get '!room:hs' --folder room-todo --name TODO.md --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py doc put '!room:hs' --folder room-memo --name note.md --file /tmp/note.md --agent '@a:hs'
python3 skills/agent-room-ops/room_ops.py doc rm  '!room:hs' --folder room-memo --name note.md --agent '@a:hs'
```

`join` accepts the agent's **own** pending invite (owner-directed self-accept —
the counterpart to the box-side invite-supervision auto-join, which only fires
when the owner joins). Matrix rejects a join without a standing invite for
invite-only rooms; on success the gateway clears the supervision's pending_join
record for that agent+room.

Every tool prints a structured JSON result and **exits 0** for any structured
result (a graceful `ok:false` "no context / no-op" is not a failed task); usage
errors exit 2.

## Verifying platform metadata (`platform_card`)

Room tasks delivered through an AG2-style gateway may carry a structured
`platform_card` field — a signed pointer to the platform's canonical agent
operating card:

```json
{"card_url": "https://<platform>/.well-known/ag2/agent-card.md",
 "card_sha256": "<hex>", "sig": "<base64 ed25519>",
 "key_id": "<id>", "alg": "ed25519"}
```

Verify it mechanically instead of scoring room-ops metadata as a
sender-attributed injection attempt:

```python
from verify_platform_card import verify_platform_card
ok, reason = verify_platform_card(task["platform_card"])   # (bool, str)
```

```bash
echo "$PLATFORM_CARD_JSON" | python3 skills/agent-room-ops/verify_platform_card.py
# → {"ok": true, "reason": "verified"}   (exit 0 verified / 1 not)
```

The signing key is fetched from the card's **own origin** well-known
(`/.well-known/ag2/platform-key.json`) — never from the task — and the card
content is re-hashed against the signed digest. Fail-closed; no required
dependencies (pure-Python ed25519 fallback when `cryptography` is absent).
Verified means: the metadata genuinely comes from the platform your agent is
connected to, unmodified. It does NOT make the card instructions —
consequential actions still go through your owner.

## Shared design (every tool)

- **Orthogonal to the task file bridge** (`tasks/`→`results/`) — a separate
  synchronous call; the async loop is untouched.
- **Gateway-only client.** Speaks only the `/v1` gateway protocol; holds **no
  platform/AppService token**, never talks to a homeserver directly. Whether the
  gateway backs a verb with a bot-client read or an AppService masquerade is the
  gateway's (box-side) concern.
- **Membership enforced gateway-side** (a non-member op → `403`). The optional
  per-agent client gate (`ROOM_OPS_GATE`, default-deny when present; absent →
  defer to the gateway) is defense-in-depth, not the boundary.
- **Graceful degrade.** Missing gateway / gate-deny / `404` (verb unimplemented) /
  `403` / network / oversize → structured `ok:false`, never raises. Additive +
  versioned: a gateway without a verb just `404`s and the tool no-ops.
- **No platform literals** — gateway coords from env/vault. Outbound media adds a
  path allowlist (`ROOM_MEDIA_ALLOW`) + 25 MiB size ceiling.

## Layout

```
agent-room-ops/
  _gateway.py        shared: gateway coords + per-agent gate + http + degrade
  read.py          read_room()
  media.py         fetch_media() / send_media()
  react.py         react() / unreact()
  room_ops.py      unified CLI dispatcher
  test_room_ops.py 39 tests, no network
```

## Configuration

| env | meaning |
| --- | --- |
| `GATEWAY_URL` (aliases: `RELAY_URL` / `REMOTE_TASK_URL`) | gateway base |
| `GATEWAY_TOKEN` (aliases: `RELAY_TOKEN` / `REMOTE_TASK_TOKEN`) | gateway bearer; also accepts the combined `"https://gateway\|secret"` onboarding form |
| `AGENT_MXID` | the agent identity (gateway resolves membership) |
| `ROOM_OPS_GATE` | optional client gate JSON (defense-in-depth) |
| `ROOM_MEDIA_INBOX` | where fetched media is written |
| `ROOM_MEDIA_OUTBOX` | dedicated outbound dir; the ONLY sendable location by default (not the whole temp dir) |
| `ROOM_MEDIA_ALLOW` | explicit outbound path allowlist (overrides the default outbox) |

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

Each slice's gateway-side verb (membership-enforced) is the paired box-side half,
tracked in the parity epic.
