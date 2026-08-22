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
  (`remote_gateway_bridge.py`, publish site) — there `item_id == task_id`,
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
the SAME side-effect — new `delivery_id`, SAME `idempotency_key`; an operator
REQUEUE of a parked item resumes the old delivery and replaces neither.
Today's shipped approximations:
`task_id`-keyed delivered-sentinels and the outbox DELIVERED terminal state.

### incarnation_id

One process lifetime. Shipped today: Design C terminal records embed
`worker/pid/start_usec`; the gateway's `_INST_SUFFIX` scopes per-instance
state. Incarnations ATTRIBUTE work ("which process performed attempt N");
they never NAME work (ratchet 1).

## The three ratchets (normative, effective immediately)

**R1 — No logical identity from process material.** New code must not derive
`delivery_id`, `task_id`, or `idempotency_key` from PID, worker name,
`start_usec`, `_INST_SUFFIX`, or any other incarnation material. Incarnation
material may appear in *attribution fields* of a record, never in its identity.
(Why: a restart would re-mint identities, and exactly-once dies at the rename.)

**R2 — delivery_id survives every resumption; only a successor mints anew.**
The operations are distinct, and each declares its identity semantics:

| operation | shipped path | delivery_id | idempotency_key |
|---|---|---|---|
| pre-terminal retry / crash recovery | outbox claim recovery | preserved | preserved |
| `PARKED` → operator requeue | `src/outbox.py` requeue (`PARKED → QUEUED`, same key) | preserved | preserved |
| post-reconciliation re-send | successor after a final disposition | NEW, with lineage to predecessor | preserved (same side-effect) |

`PARKED` is therefore a SUSPENDED disposition in this model, not a terminal
one: requeue resumes the same delivery, matching the shipped same-key
behavior. Final dispositions are `CONFIRMED` and terminal `NOT_DELIVERED`;
only after one of those does a re-send create a successor delivery.
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
- Discord's task-keyed delivered-sentinels (`src/discord-bridge.py`,
  `_delivered_sentinel_path` block) / gateway drops: current behavior unchanged;
  their implicit `delivery_id == task_id` equality is *documented debt*, legal
  under R3's mapping (`legacy: task_id @ boundary`) until slice 2.

## Non-goals of this document

No file-format changes, no new fields written, no migration, no daemon-contract
changes. Enforcement tests (a ratchet suite failing on R1/R3 violations in new
code) are slice 2's opening move, not part of the freeze itself.
