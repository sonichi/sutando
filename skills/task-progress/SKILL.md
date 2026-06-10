# task-progress

Sends mid-task progress updates to the channel a task came from (Slack, Discord, or Telegram).

## Critical rule — notify BEFORE any work begins

**Call notify.py as the FIRST action after reading a task — before transcription, web searches,
code reads, or any other tool call.** The user's first signal that you received their message
must be the notification, not silence followed by a result minutes later.

### Voice message tasks (most common failure case)

When a task contains a voice attachment (`[File attached: ...]`), notify BEFORE calling the
transcription script. Transcription takes 10–30 seconds — the user should not wait in silence.

Wrong order:
1. Read task (sees voice attachment)
2. Call transcribe.py ← 20s of silence
3. Process transcript
4. Return result ← user waited 60+ seconds with no signal

Correct order:
1. Read task (sees voice attachment)
2. **Notify: "Got your voice message, give me a moment."** ← user knows within seconds
3. Call transcribe.py
4. Process — if research needed, notify again before starting
5. Return result

### All other tasks

Wrong order:
1. Read task
2. Do research (WebSearch, WebFetch, file reads, analysis...)
3. ← user waits 2 min with no signal
4. Notify "on it"
5. Return result

Correct order:
1. Read task
2. **Notify immediately** ← user knows you got it within seconds
3. Do research / work
4. Notify at key checkpoints
5. Return result

## When to notify

Notify at task-start when any of these apply:
- Research questions (web search, reading files, looking things up)
- Code changes (editing, writing, testing)
- PRs (opening, reviewing, updating)
- Multi-step analysis (GTM strategy, architecture review, brainstorming)
- Anything that will take more than ~60 seconds before the result appears

No notification needed for:
- A factual answer you can give immediately from memory
- A one-sentence reply

**When in doubt, notify.** A false positive (notifying for a 30-second task) is far less
annoying than silence for 2 minutes on a research task.

## How to use

Read the task file to get `source` and `channel_id` (or `chat_id` for Telegram), then call
**immediately after reading the task**:

```bash
python3 $CLAUDE_CONFIG_DIR/skills/task-progress/scripts/notify.py \
  --source slack \
  --channel-id D0B5L7X2TK2 \
  --message "On it — looking into that now. Back in a minute."
```

For research tasks, be specific about what you're doing:
```bash
  --message "Researching Trigify setup time now — back in a minute."
```

For a Slack @mention (threaded reply), add `--thread-ts <ts>` to keep the update in-thread.

Mid-task checkpoint update:
```bash
python3 $CLAUDE_CONFIG_DIR/skills/task-progress/scripts/notify.py \
  --source slack \
  --channel-id D0B5L7X2TK2 \
  --message "Done with the research — writing up the summary now."
```

### Field mapping from task files

| source    | field in task file  | CLI flag        |
|-----------|---------------------|-----------------|
| slack     | `channel_id:`       | `--channel-id`  |
| discord   | `channel_id:`       | `--channel-id`  |
| telegram  | `chat_id:`          | `--chat-id`     |

Optional for Slack @mentions: `reply_thread_ts:` → `--thread-ts`

## Supported channels

- **Slack** — `chat.postMessage`, token from `$CLAUDE_CONFIG_DIR/channels/slack/.env` (`SLACK_BOT_TOKEN`)
- **Discord** — REST v10 messages, token from `$CLAUDE_CONFIG_DIR/channels/discord/.env` (`DISCORD_BOT_TOKEN`)
- **Telegram** — `sendMessage`, token from `$CLAUDE_CONFIG_DIR/channels/telegram/.env` (`TELEGRAM_BOT_TOKEN`)

## Fail-open

A failed send (missing token, network error) prints a warning to stderr and exits 1.
**Always continue working on the task regardless of exit code.** The notification is
best-effort — task delivery via the result file is the authoritative path.
