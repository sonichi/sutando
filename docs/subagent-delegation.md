# When to spawn a subagent, and how to pick its model

Extends the standing rule in CLAUDE.md / AGENTS.md: heavy work goes to a subagent by default, so the
main thread stays free to pick up new messages, make quick decisions and coordinate. It applies to
both core runtimes (Claude and Codex) and does not wait for an explicit trigger.

"at background" / "in parallel" always means spawn one (Chi 2026-08-21) — not "keep this in mind",
and not a licence to defer to a later session. Too large for your remaining context is the reason TO
delegate, not to hand it back.

## Heavy work — delegate by default

Heavy means sustained work that would hold the main thread for more than a few minutes:
investigations, test runs and CI monitoring, repository scans, evidence collection, long reviews.
While the main thread does it, every new message waits behind it. Delegate it unless it is on the
do-not-delegate list below.

Each delegation is bounded:

- **Scope** — one question or deliverable, the files or sources it may touch, and what it must not
  do (no merges, no sends, no room posts unless that is the task).
- **Checkpoint** — when and how it reports: a result to read back, or a time/step limit after which
  it returns what it has. Check it at that checkpoint; do not block the main thread waiting on it.

The main thread keeps the owner-facing parts: acknowledging, deciding, and verifying the
subagent's result before reporting it (a subagent's "done" is a claim, not evidence).

## Mechanism per runtime

- **Claude core** — the `Agent` tool with `run_in_background: true`. A foreground `Agent` call
  blocks the thread just as inline work does.
- **Codex core** — Codex's built-in multi-agent tools (`spawn_agent`, then `wait_agent` with a short
  timeout at the checkpoint; `close_agent` when done). They come from the `multi_agent` feature,
  stable and on by default in current Codex CLI; `src/agent/codex/cli/start-cli.sh` does not turn it
  off. `codex features list` shows whether it is enabled on this host.
- **Either runtime, work that outlives one turn or needs its own session** — a worker from the
  worker pool (`skills/worker-pool/`), asked through `pool_ask.py`.

Beyond the default, also delegate when:

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
Inline-with-disclosure is the satisfying branch. Say it in one line, with the constraint, e.g.
"No subagent capacity right now (quota tier), so I'm running this inline — new messages will wait
until it finishes." or "Two tasks are ahead of this one; I'll start it after them."

**Never report work as backgrounded or delegated when nothing was spawned.** Claiming delegation you
did not perform is indistinguishable from compliance when read from the outside, which is what makes
it the worse of the two failures — a reader cannot tell it from the rule being followed.
