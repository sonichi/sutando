---
name: channel-aware-watcher
description: Task watcher that appends the originating Discord/Slack channel to each TASK_FILE event, so a reply can never be mismatched to the wrong channel.
---

# Channel-aware task watcher

A thin **wrapper** around the core `src/watch-tasks-stream.sh`. It runs the core
watcher unchanged and post-processes its stdout, appending each task's
originating channel to the emitted line:

```
TASK_FILE: task-1780503892093.txt  [ch: susan_private 1507122184576045206]
```

## Why

2026-06-03 incident: a sensitive reply (an immigration letter) was matched to the
WRONG task id — one whose channel was a public triage thread — and leaked into a
public channel. Root cause was a task-id/channel mismatch in the consumer. Showing
the channel **inline with every task id** makes that mismatch impossible to miss:
the consumer sees the channel the moment the task arrives, not only after opening
the file. See memory `feedback_sensitive_content_dm_only`.

## Design (why a skill, not a core edit)

Per CLAUDE.md architecture rules, core services (`src/`) stay feature-free and
main is never edited for a feature like this. So this lives entirely in the skill:
- It does NOT modify `src/watch-tasks-stream.sh` — it execs it as a child and
  augments the output stream.
- Core detection logic (fswatch, workspace resolution, PID file, tmux wake,
  archive/rename filters) remains the single source of truth in core; this skill
  only adds the channel-lookup layer. No duplication, no drift.
- Optional: if the skill is absent, the core watcher still runs normally (just
  without the `[ch: ...]` suffix).

## Usage

Start it via the `Monitor` tool instead of the bare core watcher:

```
Monitor  command: 'bash skills/channel-aware-watcher/scripts/watch.sh'
         persistent: true
         description: 'Streaming task watcher'
```

Each event is still one `TASK_FILE: <name>` line (now with the channel suffix);
read the named file via the Read tool when a notification arrives. `[ch: <name>
<id>]` is advisory — always still verify the task's `channel_id`/`user_id` against
the content before sending any sensitive reply.
