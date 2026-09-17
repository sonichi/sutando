---
name: mention-gate
description: Turn on/off whether a message that @-tags the OWNER counts as a mention of the bot. Off (default) is today's behavior — in requireMention channels such messages never reach the fleet. On, they are ingested as tasks and audit-logged. Free-listen channels are unaffected either way.
---

# Mention Gate

Today, in every requireMention channel, a message @-tagging the owner (but not
the bot) is never ingested — it stays invisible to the fleet. This skill adds
the missing ON side:

- **ON** — an owner-tagged message from anyone but the owner counts as a bot
  mention: it passes the requireMention check and becomes a task. Every
  message pulled in this way is appended to a durable audit log so the owner
  can review exactly what the toggle admitted.
- **OFF (default)** — today's behavior, unchanged.

Never affected: messages from the owner himself (they follow the existing
rules), messages that actually mention the bot (ingested in both states, as
always), and `requireMention:false` channels (everything there already
ingests, gate on or off). Later access gates (channel allowFrom, tier
resolution) still apply to an admitted message — the gate substitutes only the
missing bot-mention, nothing else.

**Fail-closed:** the feature ADDS ingestion, so every failure reads as OFF —
missing or malformed state, an unparseable `until`, or any exception inside
the bridge's gate check leaves the ordinary requireMention rejection standing.
A broken state file must never surprise-ingest.

## Usage

```bash
python3 skills/mention-gate/scripts/mention-gate.py on              # owner tags trigger ingestion
python3 skills/mention-gate/scripts/mention-gate.py on --for 2h     # with auto-expiry back to off (30m/2h/1d)
python3 skills/mention-gate/scripts/mention-gate.py off             # today's behavior (default)
python3 skills/mention-gate/scripts/mention-gate.py status          # state + how many messages pulled in
```

## State + audit

### Scope: PER-HOST, not fleet-wide

The gate is stored under `<workspace>/state/`, which `vault.sync.exclude` lists
explicitly — the vault runs in whitelist mode and carries only `notes/`,
`talks/`, `hosts/` and the memory tree. `git ls-files state/mention-gate.json`
returns nothing on a live host. So **toggling the gate changes only the host it
was run on**; every other host keeps its own setting. Run the command once per
host. There is currently no synced fleet-policy surface to store it on.

- Gate state: `<workspace>/state/mention-gate.json` —
  `{"mentions_enabled": bool, "until": "<ISO-8601>"|null}` (`mentions_enabled`
  = this gate is on), written atomically (temp sibling + `os.replace`).
  `until` auto-expiry needs no follow-up write: once passed, the gate reads
  OFF.
- Audit log: `<workspace>/state/mention-gate-ingested.jsonl` — one fsync'd
  JSON line per admitted message `{ts, channel_id, author_id, message_id,
  body (first 120 chars)}`. `status` reports the count. The row is appended
  only AFTER the admitted task file is durably written — a sender the later
  authorization gates (channel allowFrom, global allowlist) reject leaves no
  audit row, so an unauthorized sender can never inflate the audit (#3473).

## Architecture

- **Policy owner (core):** `src/mention_gate.py` — `read_state` / `write_state`
  / `owner_tag_triggers_ingest` (fail-closed) / `message_tags_owner(mention_ids,
  text, owner_ids)` / `log_gated_ingest` / `gated_ingest_count`. Owner ids are
  parameters; the module hardcodes none.
- **Discord adapter (wired):** `src/discord-bridge.py` —
  `_mention_gate_triggers_ingest(message)`, consulted inside
  `_handle_discord_message` at the requireMention rejection site (the
  `require_mention and not bot_mentioned and not role_mentioned` branch):
  when it returns True the rejection is bypassed and the message continues
  down the existing pipeline; otherwise (including on any error) the ordinary
  skip stands. The helper returns a verdict only; the audit row is written by
  `_mention_gate_log_admission` right after `_write_task_file` succeeds.
  Owner-tagging is detected from the platform `message.mentions` array plus a
  `<@ID>`/`<@!ID>` text fallback. Owner ids come from `access.json`: a
  PRESENT `tierMap` is authoritative — only its explicit `owner` entries
  count, and an empty map means no owners (the gate never triggers);
  `allowFrom` is consulted only when the `tierMap` key is absent (legacy
  file), so read-only members are never promoted to owner for this gate.
- **ag2space/Matrix adapter (NOT wired — documented instead):** verified by
  reading the code: gateway messages become task files inside `_write_task` in
  `packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py`, which is
  package-canonical (PyPI-published, intentionally divergent from `src/`; the
  `src/remote-gateway-bridge.py` loader only injects dirs/seams and execs it —
  no per-message ingest passes through repo-level code first). The broker also
  makes the mention-match decision (`is_mention`) server-side, so wiring here
  would fork the vendored contract. Where the trigger belongs: an injectable
  ingest-trigger seam in the package (mirroring
  `local_task_protocol.set_task_stamper`), consulted where `_write_task`'s
  callers decide whether a room message is for this agent; the loader would
  bind it to `mention_gate.owner_tag_triggers_ingest` + `message_tags_owner`
  with the workspace it already resolves and the owner mxid from its channel
  config. Until that package change lands, the gate has no effect on
  ag2space/Matrix rooms.

## Tests

`tests/mention-gate.test.py` — fail-closed state contract (default/missing and
malformed state read OFF, unparseable `until` reads OFF, atomic write),
tagging detection (mentions array + text fallback), injected-now expiry
flipping ON→OFF, audit-log order + count, owner-id resolution controls (an
explicitly empty tierMap yields no owners and the gate never triggers; a
present tierMap beats allowFrom; absent tierMap falls back), the production
chokepoint matrix on `_mention_gate_triggers_ingest` (verdict-only — no audit
at verdict time; OFF and default-state reject as the fail-closed control;
tags-neither, bot-only-mention and owner-authored never trigger; any error
fails closed), full-handler behavioral drives (gate ON + authorized → task
written then exactly one audit row; gate OFF/default → neither; unauthorized
sender → neither), AST pins (gate consulted in the requireMention rejection;
audit call strictly after the task write; verdict helper contains no audit
call), and a CLI round trip.

## Live witness (post-restart)

`skills/mention-gate/scripts/witness.py case1|case2|case3 --marker <unique>` —
run on the host with the restarted bridge; the operator sends the test
message from a non-owner account and the script verifies task + audit
outcomes for: gate OFF rejects; gate ON admits an authorized tagged message;
gate ON + unauthorized sender leaves neither task nor audit.
