---
name: significance-judge
description: Judge which sutando-life timeline events are significant by spawning the host's local Sutando agent CLI as a subagent. Wired as the agent_command of sutando-life's significance step; reads the step's stdin contract, runs a fresh child-process judgment, and prints the strict JSON-array verdict.
---

# significance-judge

The Sutando-side judge for sutando-life's significance layer. sutando-life's
refresh pipeline invokes a locally configured `agent_command` to decide which
mined events matter; this skill is that command. It spawns the judgment as a
**subagent** — a fresh child process of the local agent CLI — so the core
session is never involved.

## Contract

```
python3 skills/significance-judge/scripts/judge.py < request.json > judgments.json
```

**stdin** — one JSON object (sutando-life's significance step writes this):

```json
{
  "schema_version": 1,
  "instructions": "<the caller's task contract>",
  "events": [
    {"id": "...", "ts": "...", "source": "...", "kind": "...",
     "actor_id": "...", "title": "...", "detail": "...",
     "place": "...", "url": "..."}
  ]
}
```

**stdout** — ONLY a JSON array of judgments, each referencing an input event id:

```json
[{"event_id": "...", "significance_score": 0.8, "reason": "short explanation"}]
```

On any failure — malformed stdin, agent CLI missing, subagent timeout or
non-zero exit, unparseable or off-contract output — the script exits non-zero
with the detail on stderr and writes **nothing** to stdout. The caller
validates all-or-nothing, so a partial verdict must never reach it; its
refresh is unharmed either way.

## Judgment rubric

Events score high for: impact on the project, cross-agent coordination
moments, first-time events, owner decisions, shipped milestones. Routine
chatter, heartbeats, and repetitive mechanical activity score low or are
omitted.

## How the subagent runs

Default: `claude -p --output-format text`, prompt on stdin — a fresh
`claude` child process per judgment. The prompt is bounded (per-field
truncation + a total byte cap; oldest events dropped first, input is
newest-first).

Overrides:

- `SIGNIFICANCE_JUDGE_CMD` — shell-split argv for hosts running a different
  agent CLI, e.g. `SIGNIFICANCE_JUDGE_CMD="codex exec --quiet"` for codex
  hosts. The prompt is always written to the child's stdin.
- `SIGNIFICANCE_JUDGE_TIMEOUT` — subprocess timeout in seconds, default 110
  (under the caller's default 120s `timeout_seconds` budget, so this script
  fails cleanly instead of being killed mid-write).

## Host wiring (sutando-life)

In the sutando-life checkout, gitignored `significance.local.json`:

```json
{"agent_command": ["python3", "<sutando-checkout>/skills/significance-judge/scripts/judge.py"]}
```

with `<sutando-checkout>` = this repo's absolute path on the host. Codex
hosts additionally export `SIGNIFICANCE_JUDGE_CMD` in the environment that
runs sutando-life's refresh.

## Removing the skill

Delete `skills/significance-judge/` and remove the `significance.local.json`
wiring; sutando-life's significance step then skips and its refresh is
unchanged. Core services are unaffected.
