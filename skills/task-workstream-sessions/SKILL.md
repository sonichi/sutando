---
name: task-workstream-sessions
description: Enforce Team task capabilities in the owner's selected runtime and isolate assigned owner tasks into durable provider sessions. This runtime skill is adapter-invoked; Guest keeps its established read-only Codex path and ungrouped owner tasks keep the selected core's legacy session.
user-invocable: false
---

# Task workstream sessions

This runtime skill first intercepts every explicit Team task before it can
reach the unrestricted live core. It launches a fresh instance of the owner's
configured runtime: Claude uses Claude Code's native OS sandbox and a bounded
tool set, while Codex uses its native workspace-write sandbox. Team can edit
and test inside the working repository but cannot access credentials or mutate
external systems. The Claude worker starts from an empty temporary project
identity and an empty strict MCP configuration, so it neither loads the owner's
core memory nor inherits account connectors. Literal public GitHub pull-request
URLs are mediated before the sandbox: the handler fetches bounded metadata and
diffs without credentials and supplies them as untrusted review evidence.
Private PRs remain inaccessible. A sandbox/runtime failure publishes a safe terminal result
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
