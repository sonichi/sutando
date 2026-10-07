---
name: learning-window
description: Persist collection evidence and plan unread room windows separately from learned facts.
---

# Learning window state

This optional workflow component owns learning collection checkpoints, not a
resident gateway event cursor, task inbox, or transport lifecycle. It starts no resident
daemon. Network reads delegate adapter-supplied capabilities and the existing
cloud auth/HTTP owner. Core services do not depend on it.

The dispatcher supplies current joined membership for every configured scope.
`scripts/window_state.py:plan_windows` returns each scope's earliest unread
timestamp, including the immutable bootstrap timestamp for a newly joined room
or newly configured scope. Never initialize a new room from another room's or
scope's current timestamp.

After a trusted bounded collector returns a scope-qualified receipt, the
dispatcher calls `record_collection` with the dedicated private state directory.
The writer locks and validates state, atomically persists the complete receipt,
then records collection progress only for rooms with covered available history.
Failures and exhausted page budgets leave unread rooms at their prior position.
The receipt's overall success flag cannot override a failed room. The persisted
receipt contains unlearned events and must survive until its consumer commits
them to a durable facts store.

Collection is not learning. This writer never advances a learned-facts cursor or
claims dossiers changed. A recency-only update does not consume facts. Consumer
acknowledgment and receipt retention need their own tested contract before
unattended operation; never prune unlearned receipts to make a size gate pass.

`scripts/collection_pass.py:collect_pass` accepts an explicit configured scope list
and injected membership/collector capabilities. It validates exact membership and
window binding, persists partial receipts, and admits a consumer only when every
configured scope has covered available history. It does not invoke a consumer or
acknowledge facts. The optional dispatcher delegates to this contract. Direct model-authored
receipt files are not trusted collector evidence. Same-user filesystem access can
bypass any optional writer; do not claim this is all-provider authorization or
mandatory protection of a legacy cursor.

`command_collection.py:collect_commands` invokes adapter-injected argument vectors
for each configured hostname scope with strict `rooms` and bounded `history`
commands. Credential/environment resolution belongs to those adapters. Partial
strict failures are persisted with unread coverage; command timeouts and scope
mismatches prevent consumer admission. No network command was exercised by its
offline tests; adapter deployment and scheduled live runs need separate evidence.
Scope exceptions retain their type and a fixed `error_stages` label for membership
read/validation, history read, window order/validation or receipt persistence.
Labels contain no exception text, command output or credentials; a history-read
label alone does not distinguish network failure from command-output validation.

`launch.sh manifest directory log` detaches `dispatch_collection.py` with explicit
manifest config, a dedicated private collection state directory and a caller
resolved log path. It delegates interpreter selection to the repository Python
resolver. A launch receipt means dispatcher requested, not consumer completion.
The locked dispatcher performs collection before invoking the configured consumer
argument vector and supplies every retained digest-verified bundle. No fact
acknowledgment or receipt deletion is implemented; consumer exit stays unverified.
Scope keys must match the canonical collector gateway hostname, not a Matrix
homeserver alias. Private dispatcher wiring belongs to the caller's adapter.

Consumers run in their own process group. Before final readback and lock release,
the dispatcher kills remaining group members and reaps its direct child, including
on timeout, normal exit, and SIGTERM/SIGINT cancellation. A child deliberately
escaping the group is outside this guarantee; this is not a sandbox.

The consumer is a detached session with no task inbox: its child environment
sets `SUTANDO_CORE_SESSION=0` and removes any inherited `SUTANDO_INSTANCE_ID`.
The core Stop hook recognizes that explicit non-core role without relying on a
fresh core heartbeat. Other environment and hook settings are preserved. This
role declaration is queue ownership, not an authorization or tool boundary.
The Stop hook is Claude-specific and is not installed as Codex enforcement.
A detached Codex exec adapter must explicitly configure its working directory,
receipt/checker access and private output write permission; no resident core
launcher or implicit Codex sandbox grant is supplied.

`receipt_status.py` emits canonical collection summaries from digest-verified
bundles, including exact UTC timestamps and each scope's visible population.
The dispatcher persists these summaries before attempting the consumer. They
remain collection evidence when a consumer fails, exits or writes only recency.
Optional `--claims` compares structured collection claims with that evidence;
it does not validate arbitrary consumer prose or acknowledge learned facts.

An adapter can opt into proposal retention by adding `proposal_stores` to the
manifest's `config`: a mapping from permitted person keys to observed durable
store IDs. The adapter supplies this inventory, not the consumer. Without it,
consumer invocation is unchanged. This mapping does not grant document-writing
authority or establish that a proposed interpretation is true.

For an enabled job, the dispatcher persists a fresh UUID output path before
attempting the consumer. Its machine-readable return contract lists permitted
person keys and retained receipt digests. The consumer writes exactly
`{"schema": 1, "proposals": [...]}` to that path. Each proposal contains only
`person_key`, `scope`, `receipt_digest`, `text` and `references`; each reference
contains `room_id`, `event_id` and a source `excerpt`. `scope` must equal the
selected receipt's gateway hostname, never a dossier category such as `role`.
No additional proposal fields (including `status`) are accepted. The return
contract includes the digest-to-scope mapping and these exact field lists. Include proposed facts
that cannot fit a frozen dossier. Do not substitute a success claim, store ID
or document hash. Missing, malformed or old-path output stays unknown.

After consumer exit, `consumer_return.py` validates the bounded proposal file
and delegates to `pending_candidates.py`'s locked atomic writer. Valid items
survive a partially invalid batch, with rejected indices recorded separately.
A stable candidate ID deduplicates the same scoped text and evidence across
retained bundles while preserving every contributing receipt digest. A changed
interpretation is a separate pending candidate. Receipts are not acknowledged
or deleted, and all proposals remain pending. Consumer exit and proposal-file
persistence are separate from learned facts.

`document_effect.py` compares the dispatcher's full-content hash and identity
readbacks without changing pending status. Physical retention does not prove
semantic accuracy or actor authority. Same-user file access can bypass these
helpers.

An adapter may supply `config.document_readback_argv`, an explicit read-only
command vector, together with nonempty `proposal_stores`. The dispatcher calls
this command with each person key before attempting the consumer and after its
exit or timeout. `cloud_person_read.py` is an optional edge command accepting
injected `--engine` and `--workspace`; it delegates auth and GET transport to the
existing generic cloud helper. It changes no cloud documents or credentials.
The command returns `{ok:true,body:{person:{id,slug,doc}}}` with a full explicit
document. Missing, failed, cross-identity, truncated or oversized responses are
unknown. Each call has a 15-second timeout and a 2MB response bound.

Before observations are persisted before the consumer attempt. Dispatcher state
stores identity, time, document length and full UTF-8 SHA256, without document
plaintext. Ordered before/after observations at most 300 seconds apart can
report `unchanged` or `changed_unattributed`; a changed hash does not identify
who changed the document or attest a successful intended effect. Longer or
incomplete comparisons remain unknown. Readback failures preserve valid pending
proposals and collected receipts. This optional physical retention check does
not acknowledge learned facts, change pending status, verify interpretations or
grant actor authority. Deployment and actual scheduled use require separate
evidence from local CLI fixtures.

An adapter may additionally supply `config.proposal_check_argv` with a nonempty
store inventory. The dispatcher writes a private context for the fresh output
path and retained receipts before the consumer attempt, and injects the checker
argument vector with `--context`. `check_return.py` delegates the same bounded
reader and proposal validator used by the postexit writer, returning valid
indices and rejection reasons with no pending or document writes. Consumers
can correct literal source excerpts within their original attempt. A new
attempt receives a new context and output path; missing output stays unknown.
This optional preflight neither enforces consumer adoption nor establishes
semantic truth, fact acknowledgment or an authorization boundary.

The checker also returns `receipt_population`: mechanical counts derived from
digest-verified retained receipts. `message_rows` includes overlapping historical
windows; `unique_scope_room_events` deduplicates by scope, room and event ID.
Per-scope sender counts identify accounts, not authenticated humans. Empty and
incomplete receipts remain counted explicitly. These totals describe all retained
receipts, not only the current collection window. If bounded reporting cannot
verify its inputs, its status is `unknown` and totals are omitted; proposal
acceptance and the pending writer's policy remain independent of this report.
