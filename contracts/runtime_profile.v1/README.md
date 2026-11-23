# runtime_profile.v1 contract fixtures

Which model the core and each worker run, the models its runtime offers to
switch to, and the account's plan name. A heartbeat carries it as
`runtime_profile` when its `capabilities` list `runtime_profile.v1`;
`schema.json` describes that block.

The block carries no usage and no limit state. The heartbeat's `health.quota`
(`quota_status.v1`, `contracts/quota_status.v1/`) is the one source of both.

- `heartbeat.*.json`: literal heartbeats an engine sends (Claude core with a
  Claude and a Codex worker; a Codex core; a core whose status is blocked,
  with a worker that reported nothing).
- `response.*.json`: what the broker serves the owner at
  `GET /v1/agents/{agent}/runtime-profile` for each heartbeat, received at
  1790000000 and read 30 seconds later.

Each repo that copies these files (sonichi/sutando, ag2-space/ag2space-backend,
ag2-space/cinny-webclient) pins every file's SHA-256 in its tests, so a change
is re-copied to all three or its tests fail.
