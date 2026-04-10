# Call Diagnostics & Repair

Analyze phone call observability data, detect problems, track them across calls, and recommend systematic repairs.

## Usage

```bash
python3 ~/.claude/skills/call-diagnostics/scripts/diagnose.py              # last call
python3 ~/.claude/skills/call-diagnostics/scripts/diagnose.py --all        # all calls + repair recommendations
python3 ~/.claude/skills/call-diagnostics/scripts/diagnose.py -t           # show timeline
python3 ~/.claude/skills/call-diagnostics/scripts/diagnose.py -v           # verbose (show detail)
python3 ~/.claude/skills/call-diagnostics/scripts/diagnose.py --all --tracker  # generate HTML tracker + open
```

## When to use

- **After every phone call**: run on latest call to detect issues
- **Before making fixes**: run `--all` to see persistent patterns and repair recommendations
- **Never apply ad-hoc patches** — check the repair recommendations first to understand if the problem is persistent and what the systematic fix should be

## Detections

- **Tool returned too fast** (<10ms) — likely error/not-found
- **Hallucination** — Gemini claimed action state without tool verification
- **Inline task via work** — recording/screenshot/play delegated instead of inline
- **Long delay** — >30s between user request and tool execution
- **Repeated failures** — same tool failing 3+ times
- **Timestamp lag** — caller speech logged after tool that it triggered
- **Wrong tool** — Gemini used the wrong tool for the request
- **User correction** — user explicitly corrected Sutando's behavior
- **Unmet expectation** — user repeated a request (not understood)
- **Auto-invocation** — tool called without matching user request

## Repair workflow

ALWAYS follow this workflow. Never skip steps.

1. **Diagnose**: run `--all --tracker` to see the full picture across all calls
2. **Identify persistent problems**: only fix issues that appear across multiple calls. Ignore one-offs.
3. **Find root cause**: ask "why does this happen?" not "how do I patch this instance?"
4. **Make ONE minimal fix**: prefer prompt over code, prefer removing code over adding. If >20 LOC, reconsider.
5. **Deploy and track**: restart servers, then monitor the next 3+ calls in the tracker
6. **Verify or revert**: if the issue count doesn't drop after 2-3 calls, revert and try a different approach
7. **Never modify source code for call tasks**: when a user asks to change something during a call (subtitle color, video edit), use runtime tools (ffmpeg, scripts), not code changes

## Repair types

When run with `--all`, analyzes patterns across all calls and recommends:
- **prompt** fixes — changes to Gemini system instructions or tool descriptions
- **code** fixes — changes to tool implementations (retry logic, return values)
- **architecture** fixes — structural changes needed
- **unsolvable** — inherent to the platform (e.g. STT timestamp lag)

Each recommendation includes evidence (frequency, trend), priority, and specific fix instructions.

## HTML Tracker

`--tracker` generates `/tmp/call-diagnostics-tracker.html` with:
1. Latest call timeline (color-coded)
2. Issue tracker table (last 5 calls, rows = specific tool issues)
3. Line chart (errors/warnings over time)
4. Repair recommendations (prioritized with evidence)
