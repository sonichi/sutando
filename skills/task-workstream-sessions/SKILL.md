---
name: task-workstream-sessions
description: Guard Team tasks in the owner's selected runtime and isolate assigned owner tasks into durable provider sessions. This runtime skill is adapter-invoked; Guest keeps its established read-only Codex path and ungrouped owner tasks keep the selected core's legacy session.
user-invocable: false
---

# Task workstream sessions

This runtime skill intercepts Team tasks before they can reach the unrestricted
live core only when the AG2 Space broker attests that the room's access setting
is Team. The remote gateway turns that room policy into one pre-body
`team_runtime: trusted` execution stamp. Missing, malformed, duplicate, locally
forged, or body-positioned stamps fail closed to the established read-only path;
a local owner-to-Team cap is not treated as room consent. This makes the opt-in
per room, configured alongside Owner and Guest in AG2 Space, with no host-wide
environment toggle. When opted in, the worker launches a fresh instance of the
owner's configured runtime with the normal configured workspace, tools,
integrations, network, and provider settings. A Team-specific prompt
identifies the sender as a trusted collaborator rather than the owner and
requires cautious, scoped work without disclosing credentials or unrelated owner
context. Before delivery, the handler scans the final response with Sutando's
maintained secret scanner, rejects bridge delivery-control markers, and withholds
any result containing a likely credential. Scanner/runtime failures publish a
safe terminal result and never fall through to the owner core.

This is deliberately a behavioral guardrail, not adversarial isolation. Team
can perform ordinary development and operational work that needs the owner's
installed toolchain and configured integrations. The owner accepts that trust
tradeoff. The outbound scanner checks only the final response: it cannot observe
or prevent a malicious or successfully prompt-injected task from reading a
credential, sending it over the network, mutating the host, invoking a webhook,
or causing another side effect before returning scanner-clean text. Guest
remains on the pre-existing read-only Codex delegation path carried in the
task's in-band instructions.

Future defense in depth can add AG2 Space security monitoring around this
trusted tier: centralized action telemetry, prompt-injection and anomalous-tool
signals, cross-agent incident correlation, owner alerts, and rapid credential or
agent-session revocation. Those controls would improve detection and response;
they are not implemented here and must not be treated as current guarantees.

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
