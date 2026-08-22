---
name: ag2space-room-sessions
description: Route opted-in owner tasks from AG2 Space into one standing provider session per room. This runtime skill is adapter-invoked; tasks without the exact trusted room-session policy keep the selected core's existing session.
user-invocable: false
---

# AG2 Space room sessions

This runtime skill claims only owner-tier AG2 Space tasks carrying the exact
trusted `session_scope: room` header. It hashes the Matrix room ID into a stable
key and gives that room one durable provider conversation.

Missing, malformed, or future scope values, non-AG2 sources, and Team or Guest
tasks remain unhandled and follow their established paths.

## Standing-session execution (Claude runtime)

Each opted-in room runs a **standing session**: a long-lived interactive
`claude` process in a dedicated tmux pane (skill-owned socket, one pane per
`(runtime, room)`), spawned on the room's first message and kept across
messages — the same supervised-pane pattern the Sutando core itself runs under.

Per message, `handle()` takes a short lock (spawn + inject ordering only, never
a whole turn), ensures the pane exists — respawning with `claude --resume
<recorded-session-id>` after a crash or host reboot, so the conversation
survives process death — writes a sanitized **spool prompt** (the trusted task
view; never the raw task file), injects one line pointing at it into the live
pane, detaches a per-task monitor, acks the room, and returns immediately. The
watcher is never occupied by a running turn.

**Publication ownership flips versus the old inline design:** the standing
session itself writes the result to `results/<task-id>.txt` (the spool prompt
names the exact path — which necessarily embeds the task id). The monitor is
the backstop publisher on failure, never a competing one: it only writes after
the pane is dead or was just killed.

**Why standing over per-message spawn:** a mid-turn message (including a
cancel or redirect) enters the live conversation natively instead of queueing
blind behind a lock; `tmux capture-pane` gives real mid-run observability; and
turn serialization is the session's own input queue, not a turn-length flock.

## The monitor (per injected turn)

A detached watchdog per task:

- **Heartbeats** every `SUTANDO_TIER_HEARTBEAT_INTERVAL` (120s default) to the
  room while the turn runs, dispatched off-thread so a slow notifier can never
  delay the checks below.
- **Stall** = pane content frozen for `SUTANDO_TIER_STALL_TIMEOUT` (180s) with
  no result — a live provider at least animates its spinner. The pane is killed
  first, then an honest failure result is published ("resend to continue — the
  conversation itself is preserved"); the next message respawns via `--resume`.
- **Safety ceiling** `SUTANDO_TIER_HARD_TIMEOUT` (3600s) bounds the *watchdog*,
  not the work: a still-active turn is left running with a room notice, and its
  result still lands whenever it finishes. Long work is never discarded.
- A pane that **dies** without a result gets the honest failure body after a
  short grace period.

Explicit non-goal: a runaway turn that keeps producing output forever is only
bounded by the room noticing its heartbeats — kill it manually with
`tmux -S <socket> kill-session -t <pane>` if needed.

## Codex runtime — per-message fallback

Codex tasks keep the previous per-message `codex exec` / `exec resume` path
unchanged (turn-length lock, watcher fallback on failure). Interactive codex
resume-id discovery inside a pane is unverified; moving codex onto the standing
path is follow-up work, not silently claimed here.

## State and configuration

Session IDs live in `<workspace>/state/ag2space-room-sessions.json` (shared by
both paths; the standing path reads it for `--resume` on respawn). Spool
prompts are retained under `state/ag2space-room-sessions/spool/` for debugging.
The tmux socket lives in a short per-user dir outside the workspace
(`SUTANDO_ROOM_TMUX_DIR` override) because AF_UNIX paths cap at ~104 bytes.

Timeouts default to `manifest.json` values; CLI overrides environment, which
overrides the manifest. `SUTANDO_ROOM_SPAWN_WAIT` (30s) bounds how long spawn
waits for first pane output before declaring the launch failed.

Remove this skill or revert its adapter wiring to return every task to the
legacy main session path; kill stray panes via the skill's tmux socket.
