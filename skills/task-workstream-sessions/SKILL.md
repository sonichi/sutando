---
name: task-workstream-sessions
description: Isolate already-assigned owner tasks into durable provider sessions keyed by inferred workstream. This runtime skill is adapter-invoked; ungrouped tasks keep the selected core's legacy session.
user-invocable: false
---

# Task workstream sessions

This optional runtime skill reads existing assignments from
`<workspace>/data/task-workstreams.json`; it never classifies tasks or changes
the grouping sidecar. For each assigned owner task it resumes a headless Claude
or Codex provider session dedicated to that workstream and atomically publishes
the final result body. Session IDs live in
`<workspace>/state/task-workstream-sessions.json`, so provider context remains
separate and resumable across core restarts.

Ungrouped, non-owner, invalid, or unavailable assignments fail open to the
selected core's unchanged legacy task path. If an isolated provider fails, the
watcher also falls back to the live core rather than stranding the durable task;
the log explicitly records the possible at-least-once retry.

Tradeoff: isolated workstream transcripts are headless and do not render in the
canonical Core CLI pane. Remove this skill to disable isolation without
disabling task grouping.
