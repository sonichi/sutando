# Collaboration Intelligence scheduled maintenance

Use real-time or task-driven observations as the primary update path. Scheduled work provides eventual convergence, missed-event recovery, freshness checks, and summaries.

## Recommended jobs

| Job | Default cadence | Scope | Output |
|---|---|---|---|
| `room-delta-refresh` | Every 15 minutes when no event stream exists | Fetch changes after the stored cursor for active rooms | Updated members/context; alert only on material changes |
| `small-room-roster-sweep` | Daily, staggered | Rooms with fewer than 100 members whose provider supports exhaustive roster reads | Reconciled full snapshot or recorded partial/failure |
| `room-context-rollup` | Every 6 hours for active rooms; daily otherwise | Summarize new messages since the context cursor | Decisions, blockers, handoffs, unresolved questions |
| `unknown-identity-review` | Daily | Unresolved identities and candidate cross-bridge links | Confirmed links or a deduplicated review queue |
| `large-room-reconciliation` | Weekly or trigger-only | Large rooms with count mismatch, cursor gap, stale snapshot, sensitive pending action, or unresolved key participant | Targeted/full sweep when supported |
| `scoped-collaboration-refresh` | Daily | Active PR, issue, incident, project, and feature relationships | Refresh active edges; close them when their scope reaches a terminal state |
| `priority-profile-review` | Monthly | VIP/priority designations, scopes, expiries, and handling preferences | Expire obsolete designations and flag ambiguous or unsupported priority records |
| `quick-lookup-refresh` | On every observation; trimmed hourly | The hot quick-lookup index (recent entities, active rooms, open scopes) | Promote touched entries, evict stale, keep it bounded; pin VIP/priority and open unknowns |
| `collaboration-intelligence-audit` | Weekly | All known rooms and relationship facts | Stale purpose, ownership conflicts, orphan agents, scope risks |

Treat these cadences as defaults. Back off inactive rooms and providers with rate limits; accelerate rooms involved in active incidents or sensitive coordination.

Review short-lived PR/issue/incident relationships on a daily scale and project/feature relationships on a daily-to-weekly scale. Review durable team relationships monthly or when explicit organizational evidence changes; silence alone is not a deletion signal.

## Job contract

Every job must:

1. Acquire a single-run lease or otherwise prevent overlapping execution for the same room and job.
2. Read a durable cursor/checkpoint and process only unseen evidence unless performing an explicit sweep.
3. Bound rooms, pages, messages, wall time, and provider calls per pass.
4. Persist verified updates and the new cursor atomically enough that retries remain idempotent.
5. Preserve the last known-good roster when a sweep fails or pagination is incomplete.
6. Record `started_at`, `completed_at`, source cursors, pages scanned, result, and next eligible run.
7. Deduplicate alerts. Stay silent when nothing material changed.
8. Respect source access scopes; never move private facts into a broader summary.

## Trigger an immediate maintenance pass

Do not wait for the normal cadence when:

- a new room is first observed
- reconnect or recovery may have lost events
- reported member count differs from stored membership
- an unfamiliar participant affects trust or a sensitive action
- a full AG2 Space roster or independent exhaustive query shows an agent without its expected owner; absence from a truncated task header is not evidence
- a recipient or owner must be resolved before sending
- the latest context is too stale to coordinate safely

## Provider notes

- **AG2 Space:** the **task-file stream (`tasks/` + `tasks/archive/`) is the observation source** — ingest each task's headers immediately; that is observation, not room enumeration. Drive the refresh from a **cursor over task arrival time** (the `id` epoch-ms or `timestamp:` header, never mtime) so each sweep processes only tasks newer than the last, never from scratch. Compare `len(room_members)` with `room_member_count`: equality is a full task-time snapshot; a smaller list is truncated/partial, with the true size taken from `room_member_count`. Use scheduled or on-demand reconciliation when an exhaustive roster is required.
- **Discord:** prefer channel/thread deltas. Do not repeatedly enumerate a large guild and call the result full when the bridge exposes only a partial roster.
- **Telegram:** use observed deltas and context rollups; the current bridge cannot produce a Matrix-style exhaustive membership sweep.
- **Slack:** use cursors and pagination; stagger channel sweeps and treat Slack Connect access scope carefully.
- **WhatsApp:** refresh only operationally relevant chats and minimize stored phone-number exposure.

## Degraded mode

If no scheduler exists, run the same maintenance checks opportunistically when a room is encountered. Mark the record with the last attempted maintenance time so the next invocation can continue rather than restart.
