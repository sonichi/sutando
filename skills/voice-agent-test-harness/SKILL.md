# Voice-Agent Test Harness

Drive a fixed suite of spoken tests against a voice agent ("subject") from a co-located machine ("prober"), measure response latency / clarity / accuracy, diff against baseline, and report to the owner over Telegram.

**Design:** [docs/voice-agent-test-framework.md](../../docs/voice-agent-test-framework.md)

> **v1 (macOS).** Real audio path: TTS via `gemini-tts` + `afplay`, mic capture via `sox` `rec` (CoreAudio), voice-onset via numpy RMS, STT + judge via Gemini (Sutando-standard, `GEMINI_API_KEY`). Manual trigger; reports to owner only. Each prober-side component is tested; the full closed loop needs the second laptop speaking.

## What runs end-to-end today

So that a half-SKIPPED suite is never mistaken for "mostly fine," here is exactly what executes through the real acoustic path now versus what is stubbed or excluded. A captured live run is committed at [`examples/run-2026-06-06.json`](examples/run-2026-06-06.json).

| Capability | Status today |
|---|---|
| Single-answer suite (`test_cases.yaml`, `core-v1`) — speak → capture → onset → Gemini STT → judge → score | ✅ **Wired.** Every row runs end-to-end on real audio; `pass` / `fail` / `partial` / `no_response` are all measured outcomes, not stubs. |
| Latency / clarity / accuracy scoring + baseline diff + Telegram roll-up | ✅ **Wired** — computed on real captured turns. |
| `timer` action test — real side-effect verify (waits, listens for the alarm) | ✅ **Wired.** |
| Multi-turn workflow turns (`workflow_cases.yaml`, e.g. the developer code-change flow) | ⚠️ **Partial.** The spoken handling is captured and judged; remote side effects (branch/test/cleanup) are **not observable from the prober**, so these score wording only. |
| Gmail / CRM workflow turns | ⛔ **Excluded** — unfinished test setup; omitted from results, not reported as failures. |
| Daily auto-scheduling | ⛔ **Not wired** — manual trigger only. |

## How to try it (two laptops, same room)

1. **Subject:** on laptop 2, start a normal Sutando voice session, mic open, speaker up.
2. **Prober:** on laptop 1 (this one), grant Terminal **Microphone** permission (System Settings → Privacy → Microphone), then:
   ```bash
   cd ~/GitHub/sutando/skills/voice-agent-test-harness
   python3 scripts/run_suite.py --quick        # --quick shortens the 2-min timer wait to 30s
   ```
3. The prober speaks each prompt; the subject replies; the prober measures, transcribes, judges, and prints the roll-up. Add `--deliver` to send the report to your Telegram.

Useful flags:
```bash
python3 scripts/run_suite.py --only arithmetic   # one test by id
python3 scripts/run_suite.py --dry-run           # no audio/model; canned data (CI/sanity)
python3 scripts/baseline.py --promote results/voice-test/<date>.json   # set regression baseline
```

## Preconditions (same-room run)

- Both laptops awake, unmuted, mics/speakers enabled, within normal speaking distance.
- Subject (Sutando 2) in a normal voice session with mic open.
- The runner gates on mic calibration; if the mic path is dead/clipping it reports `SKIPPED`, not a fail.

## Action tests

Tests with an `effect` block (the `timer`) verify the **real side effect**: after the verbal confirmation, the prober waits the timer duration and listens for the alarm actually firing. Confirmation without an observed effect downgrades to `partial`.

## When to use

- **Manual run** before/after a voice-pipeline change, or to spot-check responsiveness/clarity/accuracy.
- **Bring-up of a new agent** as the subject — only the `summon` test is agent-specific.
- (Daily auto-scheduling is a planned future improvement.)

## Output

- `results/voice-test/<date>.json` — per-test rows (latency, accuracy, clarity, transcript, effect) + suite roll-up (gitignored).
- With `--deliver`: a Telegram message to the owner — pass rate, p50/p95 latency, clarity, and any regressions vs baseline.
