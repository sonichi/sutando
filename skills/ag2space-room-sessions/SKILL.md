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
an owner request. A timeout after provider launch is outcome-unknown instead:
the skill checkpoints a session handle when available, records durable replay
suppression, and publishes a terminal explanation without re-running the task.

Session IDs live in `<workspace>/state/ag2space-room-sessions.json`. Remove this
skill or revert its adapter wiring to return every task to the legacy main
session path.

Provider hard and stall timeouts default to the values declared in
`manifest.json`. CLI options override environment values, which override the
manifest defaults.
