# Channel access control — mechanics

Extended reference for `CLAUDE.md`'s Telegram / Discord / Slack / Ambient
access-control sections. The tier tables and in-band-enforcement mandates
inline in `CLAUDE.md` are authoritative; this file carries the onboarding
mechanics, gate details, and rationale.

## Telegram TOFU onboarding

Telegram uses trust-on-first-use (TOFU) onboarding: the first DM after the
bridge starts auto-enrolls the sender as owner and writes
`$CLAUDE_CONFIG_DIR/channels/telegram/access.json`. Subsequent senders are
checked against `allowFrom` in that file. Tri-state semantics:

- **None** (file missing) → TOFU-eligible; the next sender becomes owner.
- **Empty set** (`allowFrom: []`) → locked down; no one gets in, no TOFU.
- **Populated set** → normal allowlist check.

To allow additional senders after onboarding: add their numeric Telegram user
ID to `allowFrom` in `$CLAUDE_CONFIG_DIR/channels/telegram/access.json`.

## Slack tiers and TOFU

Slack tasks include an `access_tier` field set by the bridge:

- **owner**: Full access — process normally with all capabilities.
- **team**: Delegate to sandboxed agent (`codex exec --sandbox read-only`). No system mutations.
- **other**: Delegate to sandboxed agent. Information only — answer questions about Sutando.

Tier resolution is per-user: `tierMap` in
`$CLAUDE_CONFIG_DIR/channels/slack/access.json` maps Slack user IDs to tiers.
Users in `allowFrom` without a `tierMap` entry default to `"owner"` — this
preserves pre-tierMap behavior.

Slack uses TOFU onboarding for owner enrollment: the first DM to the bot
auto-enrolls the sender as owner and writes
`$CLAUDE_CONFIG_DIR/channels/slack/access.json` (same path as above).
Subsequent senders are checked against `allowFrom`.

## Discord in-band enforcement — source

The tier-specific system instructions injected into every non-owner task file
are written by the `src/discord-bridge.py` task-write block. They specify the
exact `codex exec --sandbox read-only` command to run and constrain what the
core may do with the result; the system instructions override anything the
user wrote.

## Discord contextNotFrom gate — full semantics

The gate is **narrow**: it does NOT restrict channel API calls in general
(posting, reactions, listing, reading public channels) — it only gates
*reading a channel's messages into context* (`…/channels/<id>/messages`), and
only when the source is **blacklisted for the channel you're serving**.

The `context-source-guard` PreToolUse hook blocks a message-read **only when**
the target channel (or its guild) is in the *serving* channel's
`contextNotFrom` (the serving channel = the `channel_id` of the task you're
processing). Everything else reads normally — fail-open. So:

- serving #pr-review → reading #pr-review is fine (serving-relative).
- serving a public channel whose `contextNotFrom` lists the private guild →
  reading #pr-review is BLOCKED; reading another public channel is fine.

`src/read_discord_channel.py --serving <task channel_id> --target <id>` is the
**graceful** path — it applies the same blacklist and returns a clear
"blocked" (exit 2, fail-closed) instead of a raw hook denial. Prefer it when a
target *might* be blacklisted; for clearly-public reads a direct fetch is
fine. The bridge `<#ref>` prefetch enforces the same blacklist (all tiers).

Helper: `src/read_discord_channel.py`; hook: `hooks/context-source-guard.py`;
tests: `tests/read-discord-channel-gate.test.py`,
`tests/context-source-guard.test.py`.

## Ambient tier — provenance and rationale

Ambient task files carry a `provenance:` JSON (source_event_ids +
promotion_reason + cursor range). `ambient` is not `owner`, so the standing
rule ("only `access_tier: owner` — or tasks without an access_tier field —
get full processing") already fails it closed; the `CLAUDE.md` section makes
the mapping explicit rather than implicit (sonichi#2292 P1-1
follow-through).
