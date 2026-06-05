# Voice-Agent Test Harness

Drive a fixed suite of spoken tests against a voice agent ("subject") from a co-located machine ("prober"), measure response latency / clarity / accuracy, diff against baseline, and report regressions to Telegram.

**Design:** [docs/voice-agent-test-framework.md](../../docs/voice-agent-test-framework.md)

> **Draft.** The audio transport (TTS playback, mic capture, voice-onset, STT) is stubbed with `TODO(voice)` markers. The schema, scoring, aggregation, baseline-diff, and reporting are real.

## Usage

```bash
# Run the full suite (prober side, same room as the subject)
python3 ~/.claude/skills/voice-agent-test-harness/scripts/run_suite.py

# Dry run — no audio; uses canned transcripts to exercise scoring/reporting
python3 ~/.claude/skills/voice-agent-test-harness/scripts/run_suite.py --dry-run

# Run a single test by id
python3 ~/.claude/skills/voice-agent-test-harness/scripts/run_suite.py --only timer

# Promote the latest run to the regression baseline
python3 ~/.claude/skills/voice-agent-test-harness/scripts/baseline.py --promote results/voice-test/2026-06-05.json
```

## Preconditions (same-room run)

- Both laptops awake, unmuted, mics/speakers enabled, positioned within normal speaking distance.
- Subject (Sutando 2) is in a normal voice session with mic open.
- The runner gates on a `liveness` probe + audio-level calibration; if either fails it reports `SKIPPED`, not a fail.

## When to use

- **Daily** scheduled run to catch voice regressions (latency creep, dropped responses, clarity loss).
- **Before/after a voice-pipeline change** to A/B responsiveness and accuracy.
- **Bring-up of a new agent** as the subject — only the `summon` test is agent-specific.

## Output

- `results/voice-test/<date>.json` — per-test rows + suite roll-up (gitignored).
- Telegram message via the results bridge with pass rate, p50/p95 latency, and any regressions.
