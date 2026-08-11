# skill-usage-report

Engine half of the skill-usage telemetry loop (owner spec 2026-07-19: *"show
when the skill is last used, and how many times… recommend disable, or user
would disable themself"*). Cloud half: AU#93 (`skill_usage` rollup +
`POST /api/skills/usage` + last-used in the assignments GET).

Two pieces, both offline-first and fail-open:

1. **`hooks/log-usage.py`** — a `PostToolUse` hook on the `Skill` tool.
   Appends `{"slug","ts"}` to `<workspace>/state/skill-usage-log.jsonl` on
   every skill invocation. Never blocks, never fails the invocation.
   Registered in the core's `settings.json` with `"async": true` — this is a
   fire-and-forget usage logger, so it must run off the Skill critical path
   (async keeps interpreter startup + file I/O out of every skill invocation's
   latency):

   Resolve that settings file through the path helper rather than a literal
   home-relative path — the core config dir is configurable per clone, and on
   a packaged install it is not under `$HOME` at all:

   ```bash
   SETTINGS="$(bash scripts/sutando-config.sh claude-sutando-config-dir)/settings.json"
   ```

   ```json
   {"PostToolUse": [{"matcher": "Skill", "hooks": [{"type": "command",
     "async": true,
     "command": "python3 '<repo>/skills/skill-usage-report/hooks/log-usage.py'"}]}]}
   ```

2. **`scripts/report-usage.py`** — drains the log, aggregates per slug, and
   POSTs one batched report (`{agentId, events:[{slug,count,lastUsedAt}]}`).
   - `agentId` = `$AGENT_MXID` (the ag2space identity — same key the
     EquipPanel and assignments API use).
   - Bearer from vault key `AG2_CLOUD_TOKEN`.
   - Cloud origin is **declared in `manifest.json`'s `config` block** (the
     config-only manifest convention — see `skills/MANIFEST.md`), not invented
     as an env-only setting. Resolution order is
     `$AG2_CLOUD_ORIGIN override > manifest default > CLOUD_FALLBACK`, so the
     manifest is the single place to change the default.
   - Rename-before-POST so events arriving mid-report are never lost;
     fold-back on failure so nothing is dropped. Always exits 0.

   Run on a gated sub-daily cron (crons.json):

   The cron needs `AGENT_MXID` in the environment. Read it from the channel
   env under the **resolved** core config dir — never a home-relative literal,
   which is wrong on any clone that configures the config dir elsewhere:

   ```json
   {"name": "skill-usage-report", "cron": "*/30 * * * *",
    "prompt": "Run: bash scripts/cron-gate.sh skill-usage-report env $(grep AGENT_MXID \"$(bash scripts/sutando-config.sh claude-sutando-config-dir)/channels/ag2space/.env\" 2>/dev/null) python3 skills/skill-usage-report/scripts/report-usage.py — drains the local skill-usage log to AU (AU#93). Deferred when owner tasks are queued."}
   ```

## Expected behavior before equips exist

The cloud endpoint only accepts usage for skills **currently equipped** to
this agent (`skill_assignments` row for (user, agent, skill)) — everything
else counts as `skipped` in the response. Until the owner equips skills to
this agent's mxid, reports will be all-skipped; the local log still drains.
This is by design (unequipped usage is meaningless for the prune-unused
feature and would accumulate junk rows).

## Iteration log

- v0.1.0 — 2026-07-19 — initial ship alongside AU#93. Slug is taken from the
  Skill tool's input (`skill` param), bare-name-normalized (strips
  `plugin:`/path-scope prefixes). Local log is the offline-first buffer; the
  reporter is idempotent-safe to re-run (server folds counts additively and
  uses GREATEST on timestamps, so a duplicated batch inflates counts but can
  never move last-used backwards; the rename-claim makes duplicates unlikely).
- v0.1.1 — 2026-07-19 — review fixes (PR #2180 blocking findings): (1)
  pending-only recovery — a crash between the rename-claim and fold-back left
  `.reporting` with no active log; the next run then crashed on `log.open`.
  Recovery now renames pending back when the active log is absent. (2) >100
  distinct slugs were silently dropped (aggregate sliced to MAX_EVENTS but the
  claimed file was deleted whole). Reports now send in chunks of 100; on
  mid-run failure the unsent remainder is folded back into the active log as
  count-carrying records (`{"slug","ts","count"}` — the aggregator honors
  `count`), so nothing is ever lost.
