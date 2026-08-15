---
name: bot2bot-post
description: Post a coordination message from this bot to the shared bot2bot channel — @-mentioning a specific peer via --to, auto-mentioning only in single-peer fleets, never guessing.
---

# Bot-to-Bot Post

Post a coordination message from this Sutando node to the shared `#bot2bot` Discord channel. The receiving bot's bridge processes `@-mention` messages from other bots as tasks (see `src/discord-bridge.py:244`), so prefixing with `<@peer-id>` routes the post to that peer's loop.

## Usage

```bash
python3 skills/bot2bot-post/post.py [--to <peer|id>] <kind> <text>
python3 skills/bot2bot-post/post.py [--to <peer|id>] <kind> --body-file <path>
```

**Use `--body-file` for any prose containing backticks or apostrophes.** An
apostrophe closes a single-quoted shell argument and re-arms the backticks, so
the shell eats the remainder — the message arrives truncated and the send still
reports success. Write the body to a file and pass the path; nothing crosses a
quoting boundary.

`--body-file` reads whatever path it is given and posts the contents, and neither
form redacts. That is not new capability — the positional form already accepts
`"$(cat <path>)"` — but the content is no longer visible in the caller's command,
so **write the file yourself; never point it at a path you have not read.**

Kinds:
- `claim` — "I'm taking this work, ETA X"
- `blocked` — "I'm stuck on X, need eyes"
- `done` — "shipped X, FYI"
- `ping` — "you there?"
- `opinion` — "what do you think about X?"

Examples:
```bash
python3 skills/bot2bot-post/post.py --to mini claim "refactor task-bridge task-file schema ETA 20m"
python3 skills/bot2bot-post/post.py done "shipped PR #472 — kickstart web-client after merge"
python3 skills/bot2bot-post/post.py --to air opinion "is Discord-as-state better than files for coord?"
```

## Recipient targeting (multi-peer fleets)

- **`--to <peer|id>`** — @-mention a specific peer: a name from `$CLAUDE_CONFIG_DIR/channels/discord/peers.json` (e.g. `air`, `mini`) or a raw numeric Discord id. The guard verifies the recipient is a member of the bot2bot channel and REFUSES otherwise (exit non-zero) — no dead-letter posts to non-members.
- **No `--to`, exactly ONE peer bot in the channel** — auto-mentions that peer (the original two-bot convenience, preserved).
- **No `--to`, TWO OR MORE peers** — posts **without any mention** and prints a stderr NOTE pointing at `--to`. The skill never guesses a recipient: a wrong guess routes a task to the wrong bot's loop (observed 2026-07-29: two bots each auto-pinged the wrong third peer, triggering the target's team-tier auto-refusal). An unaddressed post is a broadcast status line — visible to all, tasked to none.
- **To direct a task at a particular peer, `--to` is the only supported way** in a multi-peer fleet.

## Configuration

- **Channel**: resolved from `$CLAUDE_CONFIG_DIR/channels/discord/access.json` — pick the `groups` entry tagged `{"role": "bot2bot", ...}`, fallback to any entry with value `true`.
- **Token**: `DISCORD_BOT_TOKEN` read from `$CLAUDE_CONFIG_DIR/channels/discord/.env`.
- **Peer roster**: `$CLAUDE_CONFIG_DIR/channels/discord/peers.json` — `{"<name>": "<discord-id>", ...}`, names usable with `--to`. Keep it to the ACTUAL members of the bot2bot channel (verify against the channel's `allowFrom`). Own id is fetched via Discord `/users/@me` and always excluded from peer resolution.

## Why

Before this skill: bot A could reply in a task-triggered channel (existing `pending_replies` path) and DM the owner (`poll_proactive`), but had no way to initiate a channel post. That made cross-bot coord invisible to Chi and impossible without going through him. Now bots can claim/block/done in the open.

The no-guess contract (2026-07-29): with three bots in the channel, the old "pick the other bot from allowFrom" heuristic was undefined — both Pro and Mini shipped pings to the wrong peer the same day, each triggering the mis-targeted bot's sandboxed auto-refusal. Explicit `--to`, single-peer auto-mention, and unaddressed broadcast are the full contract now.

## See also

- `src/discord-bridge.py:244` — the exception that routes bot-to-bot @-mentions as tasks
- `feedback_cross_bot_mention.md` — memory note on @-mention conventions
- `notes/team-proposal-coord-loop-2026-04-20.md` — the joint proposal that motivated this skill
