# task-progress

Sends mid-task progress updates to the channel a task came from (Slack, Discord, or Telegram).

Use this whenever a task will take more than ~2 minutes so the user knows you're working on it
and isn't left wondering if you received their message.

## When to use

**Always** call notify at task-start when the task is non-trivial (research, code changes, PRs,
anything estimated at >2 minutes). Send a progress update mid-task if the work has natural
checkpoints (e.g. "Done with X, moving to Y").

Rule of thumb:
- Simple lookup / quick answer → no notification needed
- Code change, skill build, PR, research report → notify on start + on key milestones

## How to use

Read the task file to get `source` and `channel_id` (or `chat_id` for Telegram), then call:

```bash
python3 ~/.claude/skills/task-progress/scripts/notify.py \
  --source slack \
  --channel-id D0B5L7X2TK2 \
  --message "On it — this looks like a few minutes of work. I'll update you as I go."
```

For a Slack @mention (threaded reply), add `--thread-ts <ts>` to keep the update in-thread.

Mid-task update example:
```bash
python3 ~/.claude/skills/task-progress/scripts/notify.py \
  --source slack \
  --channel-id D0B5L7X2TK2 \
  --message "Done writing the skill — running tests now."
```

### Field mapping from task files

| source    | field in task file  | CLI flag        |
|-----------|---------------------|-----------------|
| slack     | `channel_id:`       | `--channel-id`  |
| discord   | `channel_id:`       | `--channel-id`  |
| telegram  | `chat_id:`          | `--chat-id`     |

Optional for Slack @mentions: `reply_thread_ts:` → `--thread-ts`

## Supported channels

- **Slack** — `chat.postMessage`, token from `~/.claude/channels/slack/.env` (`SLACK_BOT_TOKEN`)
- **Discord** — REST v10 messages, token from `~/.claude/channels/discord/.env` (`DISCORD_BOT_TOKEN`)
- **Telegram** — `sendMessage`, token from `~/.claude/channels/telegram/.env` (`TELEGRAM_BOT_TOKEN`)

## Fail-open

A failed send (missing token, network error) prints a warning to stderr and exits 1.
**Always continue working on the task regardless of exit code.** The notification is
best-effort — task delivery via the result file is the authoritative path.
