---
name: ag2space-room-sessions
description: Route opted-in owner tasks from AG2 Space into one durable provider session per room. This runtime skill is adapter-invoked; tasks without the exact trusted room-session policy keep the selected core's existing session.
user-invocable: false
---

# AG2 Space room sessions

This runtime skill claims only owner-tier AG2 Space tasks carrying the exact
trusted `session_scope: room` header. It hashes the Matrix room ID into a stable
key and resumes one Claude or Codex provider session for that `(runtime, room)`
pair. Messages in the same room serialize through the same lock and session;
different rooms may run through the watcher's bounded parallel workers.

Missing, malformed, or future scope values, non-AG2 sources, and Team or Guest
tasks remain unhandled and follow their established paths. Provider failures
also return the task to the live core so a transient CLI failure cannot strand
an owner request.

Session IDs live in `<workspace>/state/ag2space-room-sessions.json`. Remove this
skill or revert its adapter wiring to return every task to the legacy main
session path.

Provider hard and stall timeouts default to the values declared in
`manifest.json`. CLI options override environment values, which override the
manifest defaults.

## Progress visibility

A room-session invocation is otherwise silent for its whole run — up to the hard
timeout — with nothing distinguishing "queued behind another message in this
room" from "actively working" from "hung." Three best-effort notifications close
most of that gap, each routed through the shared `task-progress` notifier using
the task's own `source`/`channel_id` headers:

- **On start** (once, the moment this invocation actually begins running, not
  when it was received) — signals the message left the lock queue and work is
  underway.
- **Heartbeat**, every `SUTANDO_TIER_HEARTBEAT_INTERVAL` seconds (manifest
  default 120s) while the provider is still running.
- **On timeout** (hard or stall) — an explicit handoff notice before the task
  falls back to the live core, so that fallback reply doesn't appear
  disconnected from the original message.

All three are best-effort and never affect the run itself: a broken or slow
notifier only costs visibility, not correctness. **Known gap, not solved
here:** the provider call itself is still killed outright on timeout with no
checkpoint or partial-result recovery — the heartbeat says the room isn't dead,
it doesn't rescue in-flight work that turns out to need longer than the hard
timeout allows.
