---
name: regression-search
description: "Search phone-call history for when a feature regressed. Classifies calls as working or broken from transcript heuristics so you don't have to read 100+ transcripts by hand."
---

# Regression Search

Searches `results/calls/calls.jsonl` for calls that touched a given feature, classifies each as working/broken from the transcript, and prints a timeline.

Closes part of [#188](https://github.com/sonichi/sutando/issues/188) — the find-regression half. Diagnose-call.py is a separate follow-up.

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

## Future work

- `diagnose-call.py` for deep single-call analysis (issue #188)
- Auto-correlate regression windows with git log
- Smarter NLP-based query matching
