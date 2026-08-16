# Identity attribution v1

This document is the canonical contract for attributing provider activity to a
Sutando principal. Provider accounts and performers are separate records: a
shared GitHub account may report an event without identifying which agent used
it.

## Invariant

A provider account establishes who the provider reported. A scoped owner policy
may classify the performer as agent or human. Only event-specific evidence may
identify the exact canonical performer.

`uses_account` is configuration evidence. It never implies that the principal
performed every event reported by that account.

## V1 scope

V1 supports canonical `human`, `agent`, `account`, `event`, and `claim` IDs.
Runtime request IDs remain receipt evidence rather than public graph entities;
interaction and collaboration IDs remain Sutando Life projections.

- `human:<uuid>` and `agent:<uuid>` are durable logical principals.
- `account:<provider>:<escaped-provider-id>` uses an immutable provider ID.
- `event:<provider>:<sha256>` is an opaque ID derived from provider, resource,
  object type, and immutable object ID. The reporting account remains evidence,
  not event identity.
- `claim:<sha256>` is derived from a caller-supplied deterministic dedupe key.

Provider object tuples contain `provider`, `account_id`, `resource_id`,
`object_type`, and immutable `object_id`. Consumers join this tuple to source
provenance; they must not join on login, display name, timestamp, or URL.

## Claim predicates

The version-1 envelope permits four predicates:

- `uses_account`: principal → account, basis `provider_auth_observed` or
  `owner_asserted`.
- `performer_kind_policy`: account → `agent|human`, basis `owner_policy`, with
  a required typed scope.
- `performed_by`: event → principal, basis `runtime_receipt` or
  `owner_asserted`, with the exact provider object tuple as evidence.
- `retracts`: claim → claim, basis `owner_asserted`. Retraction appends a new
  record and never changes existing bytes.

Policy scope names a provider and account IDs and may narrow resource IDs,
object types, exclusions for either, and inclusive RFC 3339 time bounds. An
empty optional selector means no further restriction. Broad words such as
“almost all” are not stored; exclusions must be represented explicitly.

## Storage and publication

Each host writes only its own shard:

```text
<workspace>/hosts/<host-label>/attribution/claims.jsonl
```

The production writer validates size, types, predicate signatures, and IDs;
serializes writers with an advisory lock; appends one complete JSON line;
flushes and fsyncs; and uses restrictive permissions. Duplicate claim IDs with
identical bytes are idempotent. The same ID with different bytes is corruption.
A malformed or partial existing shard fails closed rather than being rewritten.

A provider mutation and claim publication cannot be atomic. A governed executor
therefore persists its normalized receipts in the runtime SQLite outbox with the
successful request before publication. Publication uses deterministic claim IDs
and retries only the claim append. A claim failure never changes provider
success into a retryable mutation failure.

Exact receipts require a daemon-configured canonical `agent:<uuid>` identity.
The runtime never accepts performer identity from capability parameters. An
absent, legacy, or invalid daemon identity leaves exact attribution unavailable.
V1 assumes one daemon represents one logical agent; authenticated per-client
identities are a later protocol.

Direct `gh`, `git`, or other unmediated writes remain unattributed.

## Resolution and Sutando Life projection

Retractions apply before predicate-specific resolution. Provider observation is
authoritative for `reported_account_id`. Exact active `performed_by` evidence
wins for performer identity; otherwise, a matching account policy may classify
only the performer kind. Equal-rank disagreement is `conflicted` and fails
closed. Consumers retain contributing claim IDs and bases.

Sutando Life v1 keeps account graph edges for compatibility and adds these event
fields:

```text
reported_account_id
performer_id              nullable
performer_kind            agent | human | unknown
attribution_status        exact | policy-classified | unclassified | conflicted
attribution_basis         runtime_receipt | owner_policy | owner_asserted | none
attribution_claim_ids
```

Overview reports mutually exclusive `exact_agent_attributed`,
`agent_classified_exact_unknown`, `exact_human_attributed`,
`human_classified_exact_unknown`, `unclassified`, and `conflicted` buckets.
Their sum must equal the selected event count. Exact attribution coverage and
performer-kind classification coverage are separate.

## Rollout

Deploy the tolerant Sutando Life reader before Sutando writers. Missing claim
directories mean no claims. Unknown schema versions, malformed records, and
conflicts are excluded from attribution and counted in diagnostics. Both
repositories pin the same versioned fixture so contract drift is visible.

GitHub history is migrated only through an explicit scoped owner policy. V1 does
not claim exact GitHub-agent attribution until GitHub mutations use a governed
executor that emits the required provider object receipt.
