# Sparrow delivery identity — frozen definitions (B, slice 1)

Status: **FROZEN** (owner-ruled sequencing, 2026-08-22: definitions and ratchets
land before any persistent-format change references them). Amending a definition
here is a design decision requiring the owner; adding new identity kinds is not
permitted by edit — it is a new ruling.

Scope: the Sparrow edge runtime (delivery), its boundary with Sutando Server
(task), and provider side-effects. This document defines **what each identity
means and what may never produce it**. It deliberately changes no file format,
no schema, and no code path — B's later slices do that, referencing this text.

## The five identities

| identity | one line | owner | lifetime |
|---|---|---|---|
| `delivery_id` | one object crossing one boundary, once | Sparrow | from durable staging to terminal disposition |
| `task_id` | one logical unit of Sutando work | Sutando Server | from durable acceptance to task terminal state |
| `attempt_id` | one physical try at one delivery | Sparrow | one try (spawn→outcome) |
| `idempotency_key` | one external side-effect, deduplicated | Sparrow (assigned), provider (honored) | until the side-effect's dedup horizon |
| `incarnation_id` | one process lifetime of one worker | the process itself | process start to exit |

### delivery_id

One cross-boundary delivery: a specific object (task, result, event, permission
message) moving across one trust/durability boundary. **One task may produce
many deliveries** (inbound handoff, result outbound, a post-reconciliation
RE-SEND per R2's table); **one delivery may span many attempts**; a delivery
never becomes a second logical task.

- Stable across restart, crash recovery, and operator REQUEUE (R2's
  preserved rows); only a post-reconciliation RE-SEND mints a successor.
- Today's shipped approximations, PER LEG (the equality is not universal):
  the gateway result leg publishes the broker task id as `item_id`
  (`remote_gateway_bridge.py`, publish site) — there `item_id == task_id`
  **only on the unsuffixed (primary) gateway**; under `GATEWAY_INSTANCE=dev`
  the local task id is `task-dev~task-COLLIDE` while the published `item_id`
  stays `task-COLLIDE`, so the equality is per-instance, not per-leg —
  the conflation this document exists to end; the Discord proactive leg's
  `ProactiveClaimFence` constructs `item_id` as `filename#mtime_ns`
  (`src/proactive_claim_fence.py`) — a non-task identity shape already in
  production. Neither is renamed by this document.
- Receipts, parking, dedup, replay, and `sparrow inspect` all key on it.

### task_id

The Sutando-side logical work unit (`task-<id>` today). Sparrow transports and
references it but never mints it for its own accounting: a retry of a delivery
reuses the same `task_id`; Sutando must idempotently re-ack it (the ingress
linearization point is Sutando committing `task_id` to its durable store).

### attempt_id

One physical try at completing one delivery: claim → provider call → outcome.
Attempts are ordered per delivery and never reused. Today's shipped
approximation is the outbox record's `attempts` *counter* — a count, not an
identity; slice 2 gives each try a durable identity so a receipt or crash can
name *which* try it belongs to. `OUTCOME_UNKNOWN` parks the delivery precisely
because the attempt's fate is unknowable — a new attempt without reconciliation
could double a side-effect.

### idempotency_key

The dedup identity of one external side-effect (one Discord message, one
gateway POST, one file write). Distinct from `delivery_id`: a
post-reconciliation RE-SEND (the successor in R2's table) is a NEW delivery of
the SAME side-effect; an operator REQUEUE of a parked item resumes the old
delivery and replaces neither.

**Shipped key rule, which this document does NOT override:**
`delivery_core/core.py` defines the key as `f"{item_id}#{resend_epoch}"`, and
`resend_epoch` "changes only on a DELIBERATE operator re-send". A
post-reconciliation re-send IS deliberate, so on the shipped path the successor
**advances `resend_epoch` and therefore carries a NEW key** — it is not
provider-deduplicated against its predecessor. Same-key succession would be a
*change* to `docs/sparrow-v1-contract.md:24-28`, not a definition, and slice 2
must not assume it until that contract is amended.
Today's shipped approximations:
`task_id`-keyed delivered-sentinels and the outbox DELIVERED terminal state.

### incarnation_id

One process lifetime. Shipped today: Design C terminal records embed
`worker/pid/start_usec`. Incarnations ATTRIBUTE work ("which process performed
attempt N"); they never NAME work (ratchet 1).

### namespace (gateway / provider) — NOT incarnation material

`_INST_SUFFIX` is a **configured, stable namespace**, not a process
incarnation: `remote_gateway_bridge.py` derives it from the `GATEWAY_INSTANCE`
environment value, so it survives restarts unchanged. It is *required* in
logical identity because broker ids are unique only within one gateway — two
gateways can both mint `task-COLLIDE`.

Named-gateway mapping, as shipped: `_write_task()` serializes the namespaced
local id (`task-<instance>~<broker-id>`); the result leg converts back to the
bare broker id and publishes THAT as `item_id`. So a namespace-scoped upstream
id must be preserved through the round trip, never stripped as "incarnation".

## The three ratchets (normative, effective immediately)

**R1 — No logical identity from process material.** New code must not derive
`delivery_id`, `task_id`, or `idempotency_key` from PID, worker name,
`start_usec`, or any other incarnation material. Incarnation material may
appear in *attribution fields* of a record, never in its identity.
`_INST_SUFFIX` is explicitly NOT covered by this ratchet — see "namespace"
above: it is configured and restart-stable, and stripping it collides two
gateways' broker ids.
(Why: a restart would re-mint identities, and exactly-once dies at the rename.)

**R2 — delivery_id survives every resumption; only a successor mints anew.**
The operations are distinct, and each declares its identity semantics:

| operation | shipped path | delivery_id | idempotency_key |
|---|---|---|---|
| pre-terminal retry / crash recovery | outbox claim recovery | preserved | preserved |
| `PARKED` → operator requeue | `src/outbox.py` requeue (`PARKED → QUEUED`, same key) | preserved | preserved |
| post-reconciliation re-send | successor after a final disposition | NEW, with lineage to predecessor | **NEW** (`resend_epoch` advances — shipped rule above) |

`PARKED` is therefore a SUSPENDED disposition in this model, not a terminal
one: requeue resumes the same delivery, matching the shipped same-key
behavior. Final dispositions are `CONFIRMED` and the **attempt-ceiling park**;
only after one of those does a re-send create a successor delivery.
There is no shipped "terminal `NOT_DELIVERED`": `core.py` retries a confirmed
`NOT_DELIVERED` ("only a confirmed NOT_DELIVERED auto-retries"), so it is an
*attempted* outcome that re-arms the item, and exhausting the ceiling is what
produces `PARKED`.
(`backend_a.py` labels `PARKED` terminal in its own state enum; that label is
local to A's lifecycle and is superseded by this table for identity purposes.)

**R3 — Legacy artifacts map deterministically, never freshly.** A pre-B file
with no `delivery_id` gets one by a **pure function of its stable content**
(e.g. its `task_id` plus boundary name) — the same file always maps to the same
`delivery_id`, on every read, on every host. Minting a random or time-seeded id
at read time is forbidden: it would make one legacy delivery look like many.

## Relationship to shipped code (what this freezes, what it does not)

- `src/outbox.py` / vendored twin: `item_id` continues to function as the
  delivery identity for existing consumers; slice 2 introduces the explicit
  field without breaking the twin-verbatim rule.
- Design C terminal records: their embedded incarnation material is
  attribution and stays; nothing here renames their files.
- Discord's task-keyed delivered-sentinels: **superseded on current main** —
  `src/discord_result_delivery.py` makes outbox state authoritative and honors
  `state/discord-delivered/` READ-ONLY for the migration window; the bridge
  keeps `channel.send` mechanics only. Gateway drops: behavior unchanged;
  their implicit `delivery_id == task_id` equality is *documented debt*, legal
  under R3's mapping (`legacy: task_id @ boundary`) until slice 2.

## Non-goals of this document

No file-format changes, no new fields written, no migration, no daemon-contract
changes. Enforcement tests (a ratchet suite failing on R1/R3 violations in new
code) are slice 2's opening move, not part of the freeze itself.
