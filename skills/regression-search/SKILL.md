---
name: regression-search
description: "Search phone-call history for when a feature regressed (find-regression.py) and drill into a single call to see what went wrong (diagnose-call.py). Skips reading 100+ transcripts by hand."
---

# Regression Search

Two scripts for hunting down bad calls without reading every transcript:

1. **`find-regression.py`** — search `results/calls/calls.jsonl` for calls touching a feature, classify each as working/broken, print a sorted timeline.
2. **`diagnose-call.py`** — drill into a single call by SID, report refusals/errors/silences/repeated requests, optionally show metrics from `data/call-metrics.jsonl`.

Closes [#188](https://github.com/liususan091219/sutando/issues/188).

## When to use

- "When did the X feature stop working?" — pass the feature keyword.
- "Has feature Y improved?" — see the broken/working trend over time.
- Before shipping a fix — sanity check that the regression is reproducible.

## Usage

```bash
python3 skills/regression-search/scripts/find-regression.py "record"
python3 skills/regression-search/scripts/find-regression.py "summon" --since 2026-04-01
python3 skills/regression-search/scripts/find-regression.py "play" --json
```

Flags:
- `--since YYYY-MM-DD` — only show calls on/after this date
- `--json` — machine-readable output
- `--show-snippet` — print a one-line transcript snippet for each call

## Heuristics

A call is **broken** for a query if any of:
- Sutando refuses ("I can't", "I'm not able", "I'm unable", "sorry I cannot")
- Sutando reports an error ("error", "failed", "didn't work", "something went wrong")
- The user repeats the same request 2+ times in a row (Sutando didn't respond usefully)
- Sutando says "(Silence)" after the user mentions the feature

Otherwise the call is **working** if Sutando's response includes the feature keyword and isn't flagged broken.

These are intentionally crude — the goal is "good enough to find the regression window without reading 163 transcripts." Tune as you find false positives.

## Limitations

- Keyword matching only. "recording doesn't stop" vs "recording won't start" both match `record`. The issue calls this out as future work.
- No semantic understanding. A call where Sutando talks about recording but the user wanted something else still matches.
- Doesn't correlate with git commits — manual step for now.

## diagnose-call.py

```bash
python3 skills/regression-search/scripts/diagnose-call.py de1f04733fc2
python3 skills/regression-search/scripts/diagnose-call.py CA701fc4129779... --metrics
python3 skills/regression-search/scripts/diagnose-call.py de1f04733fc2 --json
```

Accepts a full SID or just the last 12 characters. Reports turn counts, refusals, errors, silences, repeated user requests, and the ending style (normal vs abrupt user end vs sutando silence). With `--metrics`, also pulls per-event tool-call timeline from `data/call-metrics.jsonl` (requires PR #223). Exit code 1 if any issues are found, 0 if clean — useful for CI.

Typical workflow: run `find-regression.py` to surface broken candidates, then `diagnose-call.py <sid>` to drill into the worst one.

## Future work

- Auto-correlate regression windows with git log
- Smarter NLP-based query matching (query: "recording doesn't stop" vs "recording won't start")
