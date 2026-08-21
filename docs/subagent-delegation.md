# When to spawn a subagent, and how to pick its model

Extends the CLAUDE.md rule that "at background" / "in parallel" means SPAWN A SUBAGENT
(Chi 2026-08-21). Beyond that explicit trigger, delegate when:

- The owner is actively engaging or the task queue is busy, and an independent workstream can
  proceed without making the main lane unresponsive. During active engagement this covers
  owner-requested work only — do not launch autonomous menu work under it.
- A task splits into two or more independent workstreams where parallelism materially cuts latency.
- Mechanical, long-running, or context-heavy work would consume the main session's context:
  repository scans, evidence collection, test monitoring, transcript summarization, bounded research.
- An independent second pass materially improves correctness: adversarial review, reproduction,
  cross-checking evidence.
- Access-control policy requires sandboxed handling of a non-owner task.

Do NOT delegate tightly coupled work, concurrent edits to the same files, irreversible actions, or
tasks whose coordination overhead exceeds the benefit. Subagents never bypass quota, presenter/pause,
or self-development gates.

## Model selection — explicit on every spawn

Cheap tier (e.g. `model: haiku`) for mechanical extraction, searching, formatting, monitoring, and
straightforward test runs. Inherit the session model for judgment-heavy work: architecture,
ambiguous debugging, security/privacy decisions, code review, synthesis. Escalate a cheap-model
result to the session model when confidence is low or consequences are material; never silently
choose a more expensive model.
