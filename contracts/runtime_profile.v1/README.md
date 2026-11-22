# runtime_profile.v1 contract fixtures

Which model the core and each worker run, the models its runtime offers to
switch to, and the account's plan and usage. A heartbeat carries it as
`runtime_profile` when its `capabilities` list `runtime_profile.v1`;
`schema.json` describes that block.

- `heartbeat.*.json`: literal heartbeats an engine sends (Claude core with a
  Claude and a Codex worker; a Codex core that reports no usage; a core at its
  usage limit with a worker that reported nothing).
- `response.*.json`: what the broker serves the owner at
  `GET /v1/agents/{agent}/runtime-profile` for each heartbeat, received at
  1790000000 and read 30 seconds later.

These bytes are identical in ag2-space/ag2space-backend and
ag2-space/cinny-webclient; each repo's tests pin every file's SHA-256, so a
change in one repo fails the other until both are re-copied. The engine copy
in sonichi/sutando joins them when its heartbeat sends the block.
