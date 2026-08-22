---
name: ag2space-room-sessions
description: Route opted-in owner tasks from AG2 Space into one durable provider session per room. This runtime skill is adapter-invoked; tasks without the exact trusted room-session policy keep the selected core's existing session.
user-invocable: false
---

# AG2 Space room sessions

This runtime skill claims only owner-tier AG2 Space tasks carrying the exact
trusted `session_scope: room` header. It hashes the Matrix room ID into a stable
key and resumes one Claude or Codex provider session for that `(runtime, room)`
pair. Messages in the same room serialize through the same session; different
rooms may run through the watcher's bounded parallel workers.

Missing, malformed, or future scope values, non-AG2 sources, and Team or Guest
tasks remain unhandled and follow their established paths.

Session IDs live in `<workspace>/state/ag2space-room-sessions.json`. Remove this
skill or revert its adapter wiring to return every task to the legacy main
session path.

## Detached execution — the provider run is never on the watcher's clock

`handle()` (what the generic watcher calls) only validates, sends an ack, spawns
a fully detached worker (`--run-detached`, `start_new_session=True`, not waited
on), and returns — it never runs the provider inline. The actual run happens in
`run_detached()`, in a process that outlives `handle()`'s own return, so a long
room turn never occupies a watcher worker slot or blocks other tasks.

Same-room messages still serialize strictly (each detached worker blocks on the
same `fcntl` lock before touching the provider session) — that hasn't changed.
What changed is *where* the wait happens: a message queued behind an earlier one
in the same room gets an explicit "queued, your turn is coming" ack instead of
the watcher silently sitting on it.

Provider hard and stall timeouts default to the values declared in
`manifest.json`. CLI options override environment values, which override the
manifest defaults. `SUTANDO_TIER_HARD_TIMEOUT` is now a safety ceiling against a
genuinely runaway process (manifest default 3600s) rather than a normal
completion boundary — since the run is off the watcher's request/response
cycle, a long-but-progressing turn is expected and not itself a problem.
`SUTANDO_TIER_STALL_TIMEOUT` (180s, unchanged) is the practical "this is
actually stuck" signal: a working provider streams some output periodically.

## Progress visibility

Three best-effort notifications, all through the shared `task-progress`
notifier (the same delivery path the `NOTIFY FIRST` convention already uses
everywhere else in this codebase):

- **On receipt** (from `handle()`, immediately) — "queued behind an earlier
  message" if another detached worker already holds this room's claim, "on it"
  otherwise.
- **Heartbeat**, every `SUTANDO_TIER_HEARTBEAT_INTERVAL` seconds (120s default)
  while the provider is actually running.
- **On failure** (provider error, hard/stall timeout) — `run_detached()`
  publishes an explicit failure result directly. This is a real behavior change
  from the old design, not just messaging: once `handle()` has told the watcher
  "0, handled," there is no synchronous caller left to fall back to the live
  core, so a detached failure is reported by publishing a plain, honest failure
  body ("...couldn't complete: `<reason>`. Resend for a fresh attempt.") rather
  than silently discarding the attempt or leaving the room with no reply at
  all. A crashed detached worker's `fcntl` lock releases automatically when the
  OS closes its file descriptors on process death — the *next* message for that
  room proceeds normally without any special recovery logic; the small
  `<runtime>-<room-key>.active` claim file is purely informational (drives the
  queued-vs-working ack), never load-bearing for correctness.

All three are best-effort: a broken or slow notifier costs only visibility.
