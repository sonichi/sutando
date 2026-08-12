---
name: task-workstream-sessions
description: Enforce Team task capabilities in the owner's selected runtime and isolate assigned owner tasks into durable provider sessions. This runtime skill is adapter-invoked; Guest keeps its established read-only Codex path and ungrouped owner tasks keep the selected core's legacy session.
user-invocable: false
---

# Task workstream sessions

This runtime skill first intercepts every explicit Team task before it can
reach the unrestricted live core. It launches a fresh instance of the owner's
configured runtime: Claude uses Claude Code's native OS sandbox and a bounded
tool set, while Codex uses a root-denied permission profile (Codex 0.132.0+).
The trusted handler projects the configured Git project's tracked working-tree
state into a disposable capsule, excluding ignored files, known credential
paths, and Sutando's private workspace/config roots. Team can edit and run
offline tests there without receiving the owner tree. When the provider exits,
the handler imports a bounded Git patch only if protected paths and concurrent
owner changes are absent. Imported new files are marked intent-to-add so a
later Team capsule can continue the work without staging their contents.

The provider retains its own authentication in the trusted client process, but
spawned commands receive a scrubbed environment, no owner account connectors,
and no network. A missing Git root, sandbox/runtime failure, unsafe symlink,
protected output, oversized patch, or import conflict publishes a safe terminal
result and never falls through to the owner core. Guest remains on the existing
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
