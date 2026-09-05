# agent-activity

Streams what the agent is doing into the room, as rows the desktop client renders in an
**events drawer** above the composer (collapsed: avatar, pulsing dots, the newest line rolling in;
expanded: the live rows of the task in progress) and in the console dock's **Events** panel
(the full history). Client side: ag2-space/cinny-webclient #888.

## Rows

One JSON object per line at `<workspace>/state/agent-activity.jsonl`, served to the client by the
loopback media route as `/media/state/agent-activity.jsonl`:

| field  | meaning |
|--------|---------|
| `ts`   | epoch seconds |
| `room` | room id the row belongs to; the drawer in that room shows it. Absent → dock only |
| `kind` | `processing` (picked the message up) · `thinking` (narration while deciding) · `working` (the tool in use, one line) · `notice` (PR heartbeats and the like) · `done` |
| `line` | the text, plain |
| `task` | `{id, from, text}` — the user message this row belongs to (`from` an mxid, `text` ≤160 chars) |
| `done` | `true` closes the task: all of its rows leave the drawer (the dock keeps them) |

A task's rows are **live** until its `done` row; a task-less row is live 15 minutes. The drawer
shows only live rows and hides itself when none is live. Rows are flat: the client marks the first
row of each task with a square dot, never folds.

## Writing rows

```bash
S=skills/agent-activity/scripts
python3 $S/activity.py append "picked up your message" --kind processing \
    --task-id task-… --from '@owner:server' --text "<their message>" [--room '!room:server']
python3 $S/activity.py append "PR #123 CI green" --kind notice
python3 $S/activity.py done "the drawer is one rolling line" --task-id task-…
```

Prefer `--task-file <workspace>/tasks/task-….txt`: it fills the task id, `from`, `text` and the room
from the task's own headers, so a row is always tagged with the room the message came from. Without
it, `--room` defaults to the room of the owner's latest AG2 Space message (`state/last-owner-activity.json`).
Write the `processing` row the moment a message is picked up and the `done` row when its result
is written; a message with neither never appears in the drawer.

## Working and Thinking rows, automatically

```bash
python3 skills/agent-activity/scripts/activity-tail.py --daemon
```

follows the newest Claude Code transcript under `<workspace>/.claude-sutando/projects/` and emits
a `working` row per tool call (its one-line description; Read/Glob/Grep/TodoWrite skipped; a call
that names a task file gains `from <sender>: <first 20 chars>`) and a `thinking` row per line of
narration, attached to the open task. With no open task it emits nothing. The transcript's
thinking blocks hold a signature and no text, so narration is the closest available signal.
Pidfile `state/activity-tail.pid`; log `state/activity-tail.log`.
