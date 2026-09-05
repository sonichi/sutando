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
With the hook installed, `processing` and `done` are written for you (see below); hand-written rows
are for anything extra the owner asks to see.

## Working and Thinking rows, automatically (hooks)

The skill declares three Claude Code hooks in `manifest.json` (`./hooks/activity-hook.py` on
`PreToolUse`, `PostToolUse` and `Stop`); `bash src/install-claude-hooks.sh` registers them like every other
skill hook. The hook never depends on the agent remembering anything:

- **Processing, automatically.** The first `PreToolUse` in a session whose input names a task file
  (`tasks/task-….txt`, a Read or a shell command) binds that task to the hook's own `session_id` in
  `state/agent-activity.sessions.json` and writes its `processing` row from the task's headers (room,
  sender, text). The agent's own `activity.py append --kind processing` still binds, for hand-written rows.
- **Done, automatically.** A `PostToolUse` whose input wrote `results/task-….txt` (Write, or a shell
  redirect) writes the task's `done` row, only from the session the task is bound to and only if the
  result file exists once the tool has run: a denied or failed write closes nothing.
- **Working.** Every later `PreToolUse` in that session (Read/Glob/Grep/TodoWrite skipped) becomes a
  `working` row for the open task bound to it: the description's first sentence, 100 characters.
- **Thinking.** `Stop` reads the last assistant text of *this session's* `transcript_path`
  (complete lines only, so a row mid-write is never split) and writes it as `thinking`.
- **Scope of task ids.** `task-<hex>` and `task-chat-…` files are tasks; `task-cron-*`, `task-bench-*`,
  `task-workstream-*` and `task-project-grouping-*` are the core's own bookkeeping and produce no rows.
  A result whose body starts with `[no-send]`, `[REPLIED]` or `[deduped: …]` closes the task with
  "closed, no message sent from here", never "replied".
- **Bounded.** The live log keeps the newest 400 rows (older rows move to `agent-activity.archive.jsonl`),
  and the session bindings file drops a task once its done row exists, so the per-tool-call reads stay small.
- **Fail closed.** A session with no bound open task writes nothing; a task another session claimed
  is never written to, so narration cannot cross rooms. The writer's own calls never become rows.
- The hook exits 0 on every path; it must not block the tool it observed.

The private thinking blocks in the transcript hold a signature and no text; narration is the
nearest available signal. The agent writes nothing it can forget: `processing` and `done` come from the hook. Hand-written
`thinking`/`notice` rows remain the personalisation surface.
