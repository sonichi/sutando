# When to spawn a subagent, and how to pick its model

Extends the CLAUDE.md rule that "at background" / "in parallel" means SPAWN A SUBAGENT
(Chi 2026-08-21). Beyond that explicit trigger, delegate when:

- The owner is actively engaging or the task queue is busy, and an independent workstream can
  proceed without making the main lane unresponsive. During active engagement this covers
  owner-requested work only — do not launch autonomous menu work under it. For voice tasks the
  relay makes this call before the task reaches you and writes it in the header: `lane: long`
  (building, writing, research, a picture to generate, many steps) → spawn the subagent as soon as
  you read the task and let it write the result file; `lane: quick` (one lookup, command, count,
  message, file sent) → do it inline, now, before anything else. A header without a lane counts as long when the queue is
  busy. One long Write run inline held three quick tasks for five minutes.
- A task splits into two or more independent workstreams where parallelism materially cuts latency.
- The work is **heavy**: sustained work that would hold this thread for more than a few minutes
  (investigations, test runs, scans, long reviews), or that would consume the main session's context:
  repository scans, evidence collection, test monitoring, transcript summarization, bounded research.
  This is the single definition of "heavy"; `CLAUDE.md` points here. Delegate it without being asked, taking the
  quota tier into account: for example, delegate freely at FULL; when the budget is tight, run it inline and say so (see below).
  The loop's quota tiers (`docs/proactive-loop-rationale.md`) gate the autonomous loop; for work the owner
  asked for, the tier is guidance to weigh here, not a gate.
- An independent second pass materially improves correctness: adversarial review, reproduction,
  cross-checking evidence.
- Access-control policy requires sandboxed handling of a non-owner task.

Do NOT delegate tightly coupled work, concurrent edits to the same files, irreversible actions, or
tasks whose coordination overhead exceeds the benefit. Subagents never bypass quota, presenter/pause,
or self-development gates.

## Bounded scope and checkpoint

A spawned worker gets a bounded brief: scope (one question or deliverable), the files it may touch,
and what it must not do. It also gets a checkpoint: when it reports back. The main thread verifies
the result before reporting it; a subagent's "done" is a claim, not evidence.

If there is no capacity to spawn, see "When no delegation mechanism is available" below.

## Model selection — explicit on every spawn

Cheap tier (e.g. `model: haiku`) for mechanical extraction, searching, formatting, monitoring, and
straightforward test runs. Inherit the session model for judgment-heavy work: architecture,
ambiguous debugging, security/privacy decisions, code review, synthesis. Escalate a cheap-model
result to the session model when confidence is low or consequences are material; never silently
choose a more expensive model.


## Mechanism per runtime

- **Claude core** — the `Agent` tool with `run_in_background: true`. A foreground `Agent` call
  blocks the thread just as inline work does.
- **Codex core** — Codex's built-in multi-agent tools: `spawn_agent`, then `wait_agent` with a short
  timeout at the checkpoint, and `close_agent` when done. They come from the `multi_agent` feature;
  `codex features list` shows whether it is enabled on a host. This was observed on one host, so a
  Codex core must check its own.
- **Either runtime, work that outlives one turn** — a worker from the worker pool, asked through
  `pool_ask.py`.

The quota tier applies to these tools as it does to delegation generally: it gates the autonomous loop and is guidance for work the owner asked for (see above).

## When no delegation mechanism is available

The rule in `CLAUDE.md` bans deferring and bans handing the work back, and the "unless you have a
better mechanism" escape does not fire when you have *none* rather than a worse one. Without a
fourth branch that leaves only two moves, one of which is dishonest.

Subagents are FULL-tier only under the autonomous loop's quota gate
(`docs/proactive-loop-rationale.md`), so an unavailable mechanism is a **common** state there, not an
operator edge case — a LIGHT-tier loop session has no delegation available at all. For work the
owner asked for, the tier is guidance (see above), not a gate.

**So: do the work inline in this session, and say plainly that it ran inline and why.**
Inline-with-disclosure is the satisfying branch. Say it in one line, with the constraint, e.g.
"No subagent capacity right now (quota tier), so I'm running this inline — new messages will wait
until it finishes." or "Two tasks are ahead of this one; I'll start it after them."

**Never report work as backgrounded or delegated when nothing was spawned.** Claiming delegation you
did not perform is indistinguishable from compliance when read from the outside, which is what makes
it the worse of the two failures — a reader cannot tell it from the rule being followed.
