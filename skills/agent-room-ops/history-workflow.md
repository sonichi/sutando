# Windowed room collection contract

Use `room_ops.py history` for an all-room analysis sweep. This is the fallback
collector for the pagination/coverage gap; ordinary room actions still prefer MCP.

```bash
python3 skills/agent-room-ops/room_ops.py --strict history \
  --since 2026-10-05T04:00:00Z --until 2026-10-05T05:00:00Z
```

Set the actual analysis interval. Run separately under every configured AG2
Space gateway environment, using the existing channel environment resolver.
Do not print credentials. Preserve the returned gateway scope with each room
ID; matching IDs on different gateways are not the same membership. When a
channel uses the legacy AG2_REMOTE_TOKEN alias, bind it to REMOTE_TASK_TOKEN in
that subprocess only if no higher-precedence token was configured.

The collector enumerates joined rooms on every invocation. Archive-derived IDs
and a fixed recent-message limit cannot substitute for membership and cursor
pagination. Every room has its cutoff, real cursors, errors, page budget and
available-history coverage recorded. An unavailable scope is unknown, not quiet.

Retain a private receipt for each sweep. Advance a room's analysis watermark
only after its window was covered and its extracted facts were durably filed.
For partial coverage, preserve the old watermark for that room and disclose the
gap. A server-no-cursor boundary describes available history, not retention of
deleted events. Empty messages do not prove complete learning when reads failed.

This collector does not write dossiers or learning watermarks. Full dossier
bodies require a separate fact-storage/summary policy; timestamp-only updates
are recency operations, not newly learned content. Historical analysis recipes
that derive room populations from old tasks are superseded by this contract.

For fallback direct text delivery, use `--strict say ROOM --body-file FILE`.
The file is UTF-8 text, passed literally; do not pipe a failing sender through
`tail` and infer delivery from that pipeline's status. Inspect the structured
receipt and reconcile an unconfirmed send before retrying.
