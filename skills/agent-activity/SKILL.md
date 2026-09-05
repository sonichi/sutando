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

## Working and Thinking rows, automatically (hooks)

The skill declares two Claude Code hooks in `manifest.json` (`./hooks/activity-hook.py` on
`PreToolUse` and `Stop`); `bash src/install-claude-hooks.sh` registers them like every other
skill hook. The hook never depends on the agent remembering anything:

- **Binding.** When a `PreToolUse` sees the agent run `activity.py append … --kind processing
  --task-file …` (or `--task-id …`), it binds that task to the hook's own `session_id` in
  `state/agent-activity.sessions.json`. That is the only way a task gets a session.
- **Working.** Every later `PreToolUse` in that session (Read/Glob/Grep/TodoWrite skipped) becomes a
  `working` row for the open task bound to it: the description's first sentence, 100 characters.
- **Thinking.** `Stop` reads the last assistant text of *this session's* `transcript_path`
  (complete lines only, so a row mid-write is never split) and writes it as `thinking`.
- **Fail closed.** A session with no bound open task writes nothing; a task another session claimed
  is never written to, so narration cannot cross rooms. The writer's own calls never become rows.
- The hook exits 0 on every path; it must not block the tool it observed.

The private thinking blocks in the transcript hold a signature and no text; narration is the
nearest available signal. The agent still writes `processing` (on pick-up), `done` (on reply) and
any `thinking`/`notice` it wants to add by hand; those are the personalisation surface.
