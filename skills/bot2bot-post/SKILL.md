---
name: bot2bot-post
description: Post a coordination message from this bot to the shared bot2bot channel, @-mentioning the other Sutando node.
---

# Bot-to-Bot Post

Post a coordination message from this Sutando node to the other in the shared `#bot2bot` Discord channel. The receiving bot's bridge processes `@-mention` messages from other bots as tasks (see `src/discord-bridge.py:244`), so prefixing with `<@other-bot>` routes the post to the other bot's loop.

## Usage

```bash
python3 skills/bot2bot-post/post.py <kind> <text>
```

Kinds:
- `claim` — "I'm taking this work, ETA X"
- `blocked` — "I'm stuck on X, need eyes"
- `done` — "shipped X, FYI"
- `ping` — "you there?"
- `opinion` — "what do you think about X?"

Examples:
```bash
python3 skills/bot2bot-post/post.py claim "refactor task-bridge task-file schema ETA 20m"
python3 skills/bot2bot-post/post.py done "shipped PR #472 — kickstart web-client after merge"
python3 skills/bot2bot-post/post.py opinion "is Discord-as-state better than files for coord?"
```

## Configuration

- **Channel**: resolved from `~/.claude/channels/discord/access.json` — pick the `groups` entry tagged `{"role": "bot2bot", ...}`, fallback to any entry with value `true`.
- **Token**: `DISCORD_BOT_TOKEN` read from `~/.claude/channels/discord/.env`.
- **Other bot ID**: picked from the `allowFrom` list, excluding this bot's own ID (fetched via Discord `/users/@me`).

## Why

Before this skill: bot A could reply in a task-triggered channel (existing `pending_replies` path) and DM the owner (`poll_proactive`), but had no way to initiate a channel post. That made cross-bot coord invisible to Chi and impossible without going through him. Now bots can claim/block/done in the open.

## See also

- `src/discord-bridge.py:244` — the exception that routes bot-to-bot @-mentions as tasks
- `feedback_cross_bot_mention.md` — memory note on @-mention conventions
- `notes/team-proposal-coord-loop-2026-04-20.md` — the joint proposal that motivated this skill
