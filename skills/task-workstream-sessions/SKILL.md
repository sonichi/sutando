---
name: task-workstream-sessions
description: Enforce Team task capabilities in the owner's selected runtime and isolate assigned owner tasks into durable provider sessions. This runtime skill is adapter-invoked; Guest keeps its established read-only Codex path and ungrouped owner tasks keep the selected core's legacy session.
user-invocable: false
---

# Task workstream sessions

This runtime skill first intercepts every explicit Team task before it can
reach the unrestricted live core. It launches a fresh instance of the owner's
configured runtime: Claude uses Claude Code's native OS sandbox and a bounded
tool set, while Codex uses a named workspace-write permission profile with
enforced secret-file deny globs (Codex 0.132.0+). Team can edit
and test throughout the owner-configured working directory—the same workspace
the owner's core uses—rather than being confined to the Sutando source checkout.
The provider starts without owner account connectors and cannot access credentials
or mutate external systems. A sandbox/runtime failure publishes a safe terminal result
and never falls through to the owner core. Guest remains on the pre-existing
read-only Codex delegation path carried in the task's in-band instructions.

For owner tasks, the skill reads existing assignments from
`<workspace>/data/task-workstreams.json`; it never classifies tasks or changes
the grouping sidecar. For each assigned owner task it resumes a headless Claude
or Codex provider session dedicated to that workstream and atomically publishes
the final result body. Session IDs live in
`<workspace>/state/task-workstream-sessions.json`, so provider context remains
separate and resumable across core restarts.

Ungrouped, invalid, or unavailable owner assignments fail open to the selected
core's unchanged legacy task path. If an isolated owner provider fails, the
watcher also falls back to the live core rather than stranding the durable task;
the log explicitly records the possible at-least-once retry. This owner-only
fallback never applies to Team tasks.

Tradeoff: isolated workstream transcripts are headless and do not render in the
canonical Core CLI pane. Remove this skill to disable isolation without
disabling task grouping.
