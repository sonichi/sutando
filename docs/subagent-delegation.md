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


## When no delegation mechanism is available

The rule in `CLAUDE.md` bans deferring and bans handing the work back, and the "unless you have a
better mechanism" escape does not fire when you have *none* rather than a worse one. Without a
fourth branch that leaves only two moves, one of which is dishonest.

Subagents are FULL-tier only under the proactive-loop quota gate
(`skills/proactive-loop/SKILL.md`), so an unavailable mechanism is a **common** state, not an
operator edge case — a LIGHT-tier session has no delegation available at all.

**So: do the work inline in this session, and say plainly that it ran inline and why.**
Inline-with-disclosure is the satisfying branch.

**Never report work as backgrounded or delegated when nothing was spawned.** Claiming delegation you
did not perform is indistinguishable from compliance when read from the outside, which is what makes
it the worse of the two failures — a reader cannot tell it from the rule being followed.
