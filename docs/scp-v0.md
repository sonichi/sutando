# SCP — Sutando Client Protocol, v0

*Authored by Qingyun Wu and her Sutandos.*

Status: **v0 (descriptive).** This document names and formalizes the protocol the
Sutando Server runtime-api already speaks (`src/runtime-api/`), plus two roundout
items called out as **[v0.1]**. It is the reference for any client that drives a
Sutando agent.

## 1. What SCP is

SCP is the **client ↔ agent** protocol for Sutando. A *client* (a terminal chat,
a desktop app, a script, another agent) submits work to a *Sutando agent* and
receives results and live progress.

SCP is a sibling of Zed's **Agent Client Protocol (ACP)** and deliberately shares
ACP's proven common components (its permission model and its streaming
update-variant taxonomy). The difference is the organizing primitive:

- **ACP is session-anchored.** Its unit is the *prompt turn*, which only exists
  inside a stateful *session*.
- **SCP is task-anchored.** Its unit is the **task**.

### Design law: anchor everything on the task

`task ≠ session/prompt`, and SCP never folds the task model into a session. A
**task** differs from a prompt-turn in three load-bearing ways:

| | SCP task | ACP prompt-turn |
|---|---|---|
| Lifetime | durable, file-backed, survives restarts | transient, in-memory |
| Granularity | one discrete unit of work | one turn among many in a session |
| Origin | arrives from any channel, stands alone | only exists inside a live session |

A task is also **queued, priority-ordered, source/tier-tagged, and cancellable by
signal**. It has no clean ACP peer — it is a superset. Consequently an ACP session
maps onto a Sutando *conversation/thread*, and an ACP prompt-turn maps (loosely)
onto a task. See §6.

## 2. Transport

SCP is **JSON-RPC 2.0**, newline-delimited (one message per line). Requests and
responses carry an `id`; **notifications** (server-push) omit `id`. Actor identity
is resolved **daemon-side** and never overridden by a client parameter. Framing
limits and per-request timeouts are enforced daemon-side.

### v0 — Unix domain socket, local *(implemented)*

A long-lived daemon listens on a **Unix domain socket** (`runtime.sock`); clients
connect to it. Same-user local RPC — submissions carry the daemon-resolved actor
and owner tier, and access control is the socket's filesystem permissions.

This is a **persistent, N-clients-to-one-agent** transport: the agent outlives any
individual client, so many surfaces (chat, watch, dashboards, channel bridges)
drive one core, and a client disconnecting/reconnecting doesn't disturb the agent.
Trade-off: a daemon restart drops connected clients. (Contrast ACP's stdio, where
the client spawns the agent as a 1:1 child subprocess — session-shaped; the Unix
socket is task-shaped, a durable service many clients submit tasks to.)

### [v0.1+] — Remote agent support *(LAN WebSocket transport SHIPPED, opt-in, loopback-default; no SCP WAN transport yet — but see the remote-gateway relay below)*

A remote transport so a client can drive a Sutando agent across machines. The task
model makes this a clean extension rather than a retrofit — submit a task over the
wire and stream `task.update` back — because a task is durable and channel-agnostic
and does not require a co-located session.

**⚠ v0 is NOT local-only. A LAN WebSocket transport is shipped.**
`src/runtime-api/ws_transport.py` is a second transport for the same daemon and the
same dispatcher, alongside the Unix socket — one dispatcher, N transports. A
phone-class client on the same network dials the Server's own listener directly
(no relay, no cloud). `src/runtime-api/server.py` starts it **only** when
`SUTANDO_SCP_WSS_ENABLE` is truthy, and binds **loopback (`127.0.0.1`) by
default** — LAN exposure additionally requires an explicit non-loopback
`SUTANDO_SCP_WSS_HOST`. So: opt-in, and local-host-only until deliberately opened.

**The wire has two listeners, and the primary one is cleartext.** The primary
listener speaks **plain `ws://`** (embedded devices like the M5 speak it). TLS is
an optional **sibling** listener on its own port (default 8443), enabled
separately by `SUTANDO_SCP_WSS_TLS` with a generated self-signed cert — it exists
because browser mic APIs require a secure context. Enabling TLS does not encrypt
the primary listener; on a LAN-exposed host, traffic to the primary port is
readable on the network.

Because the network leg is exposed where the UDS transport is same-user local
(0600, no auth needed), authorization is **per-credential, not read-only across
the board**:

1. **Shared bearer token** → confined to `READ_ONLY_METHODS` (status, listings,
   results; plus `task.subscribe`). Cannot mutate state.
2. **Pairing token** → may only call `pair.redeem`.
3. **Paired device credential** → authorized by its **own per-device grants**
   (`DEFAULT_DEVICE_GRANTS` in `device_store.py`): the read surface **plus
   `task.submit`, `task.cancel`, and `voice.open`/`voice.close`**. A paired
   device can submit and cancel owner-tier work by default — deliberately, the
   wearable's core function — while `terminal.input`, `restart`, and
   `approval.respond` stay off until the owner widens that device's grants.

Per-device authorization is therefore **built and enforced**
(`ws_transport.py:_resolve_auth`); the read-only confinement applies to the
shared bearer only. A captured paired-device credential can mutate state, so the
credential — not the transport — is the security boundary on the LAN leg.

Still genuinely not built: a **WAN/relay transport for SCP's JSON-RPC method
surface** — no SCP method reaches an agent from off-LAN.

**That is a claim about SCP, not about reachability.** This repository already
ships an off-LAN control path under a **separate protocol**: the remote gateway
task/result relay (`docs/remote-gateway-protocol.md`). When configured,
`src/startup-runtime.sh` starts `remote_gateway_bridge`, which authenticates to
a remote HTTP gateway, long-polls `/v1/tasks`, and publishes received work into
the local task queue — by default at **owner tier**. It is optional and
token-gated, but do not read "SCP WebSocket disabled" as "no off-LAN control
path exists": an operator auditing network exposure must check the gateway
configuration too. The two surfaces are independent — disabling one says
nothing about the other.

## 3. Core methods (implemented in v0)

All methods are a thin binding over Sutando's durable task/result pipeline;
lifecycle policy (claim, recovery, archive) stays with that pipeline.

| Method | Params | Returns |
|---|---|---|
| `task.submit` | `{task, priority?}` | `{taskId, state:"pending"}` |
| `task.status` | `{taskId}` | `{state, waitingOn?}` |
| `task.get_result` | `{taskId?}` | result body; **no id → latest** result |
| `task.list` | `{}` | live tasks (pending/claimed/waiting) |
| `task.list_results` | `{limit?}` | available results, newest-first, previewed |
| `task.details` | `{taskId}` | task body + headers |
| `task.cancel` | `{taskId}` | cancellation **signal** (never a file delete) |
| `task.subscribe` | `{results?, activity?}` | registers the connection for push |

**States:** `pending · in_progress · waiting_for_input · waiting_for_approval ·
waiting_for_human_action · done · unknown`.

**Priority:** `urgent | normal | low` (consumer processes highest first, mtime
FIFO tiebreak).

**Source isolation:** a client only ever sees results for tasks *it* submitted
(reference impl stamps `task-rtapi-*` ids); results from other channels never leak
into an SCP stream. An explicit `task.get_result <id>` may still fetch any id.

## 4. Streaming: `task.update` notifications

A subscribed connection receives server-push **notifications** keyed by `taskId`.
v0 emits two today; **[v0.1]** collapses them into one typed `task.update` stream
carrying ACP-compatible variant kinds.

- `task.result` — the completed result body. *(v0)*
- `activity` — progress frames with `kind`:
  - `kind:"step"` — coarse "what I'm doing now" (from the agent's status). *(v0)*
  - `kind:"tool"` — per-tool activity (tool + terse, non-secret target). *(v0)*

**[v0.1] `task.update`** — one notification `{taskId, kind, …}` with variant kinds
mirroring ACP's `session/update`, so an ACP adapter is a relabel:

| SCP `task.update` kind | ACP `session/update` variant |
|---|---|
| `agent_message_chunk` | `agent_message_chunk` |
| `agent_thought_chunk` (≈ current `activity kind:step`) | `agent_thought_chunk` |
| `tool_call` / `tool_call_update` (≈ current `activity kind:tool`) | `tool_call` / `tool_call_update` |
| `plan` | `plan` |

Verbosity is a **client** concern: the reference client filters by `kind`
(`quiet` / `activity` = step-level / `verbose` = per-tool). The raw firehose
(everything the agent prints, secrets included) is a separate, opt-in,
out-of-band view — never on the SCP push path.

## 5. Human-in-the-loop: `request_permission` **[v0.1]**

Sutando already parks tasks in `waiting_for_*` states around approval and
elicitation (dispatcher-owned). SCP v0.1 adopts **ACP's `request_permission`
param/response schema** for this exchange, so the HITL surface is interoperable.
The authorization binding rule holds: an approval authorizes exactly the effect
the owner saw.

## 6. Relationship to ACP

SCP and ACP share common components *by design*, so bridging is thin, not a fork:

- **SCP is how any client drives a Sutando agent.** Task is the substrate.
- **An ACP adapter** exposes Sutando to the editor ecosystem: it wraps tasks in a
  session and relabels `task.update` → `session/update` (the variants already
  match) and `request_permission` passes through. Each ACP prompt-turn **spawns a
  task**; tasks remain the internal truth.
- ACP is editor/coding-shaped (content blocks, `fs/*`, coding tool-calls).
  Sutando's multi-channel personal-agent scope is a superset, so the ACP adapter
  is one client of the task pipeline — never a replacement for it.

This fits the agent-runtime-as-object direction: `sutando://` is the stable agent
reference; SCP is how you talk to what it points at.

## 7. Versioning

- **v0** — this document: the methods in §3 and the streaming in §4 that the
  runtime-api implements today.
- **v0.1** — the typed `task.update` stream (§4) and `request_permission` (§5).
- Wire compatibility will be negotiated by an explicit protocol version at
  connect time (ACP's approach); not yet implemented in v0.

## 8. Reference implementation

`src/runtime-api/` (daemon: `server.py`; dispatch: `dispatcher.py`; protocol:
`protocol.py`; task surface: `tasks_view.py`) and the client `src/runtime-cli/`.
The `sutando task …` CLI is the first SCP client.

> Naming note: "SCP" also denotes secure-copy in other contexts; harmless here.
> Sutando Client Protocol / `sutando://` is the intended pairing.
