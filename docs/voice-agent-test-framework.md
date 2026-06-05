# Voice-Agent Test Framework

**Status:** Draft for review. Design + skeleton; voice-transport integration points are stubbed.
**Date:** 2026-06-05
**Owner:** Vasiliy Radostev
**Companion:** [test-plan.md](test-plan.md) — broader system test plan. This doc is the *voice-responsiveness* slice of it.

---

## Goal

Validate the quality of a voice agent (Sutando, or any comparable assistant) along three axes:

1. **Responsiveness** — how fast it starts replying after you stop talking.
2. **Clarity** — how intelligible and well-formed the spoken reply is.
3. **Accuracy** — whether the reply (or the action it triggers) is correct.

Initial intent: a fixed suite of ~12–15 core tests, run **daily**, with results reported to developers over Telegram and **regressions flagged** against the previous run.

Non-goals for v1: load testing, multi-language, adversarial robustness, full task-side-effect verification (we verify verbal confirmation, not the downstream effect). These are noted as future work.

---

## Topology

Two machines in the **same room**:

- **Prober** — *Sutando 1* on laptop 1. The test driver. It speaks each prompt aloud through its **speaker**, listens for the subject's reply through its **microphone**, measures, scores, and reports. The prober is *not* a normal Sutando session — it runs the test harness (this skill).
- **Subject** — *Sutando 2* on laptop 2. A normal, unmodified Sutando voice session with its mic open. It hears the prober's speaker, responds through its own speaker. **The subject is unaware it is under test** — that is the point; we measure the real product path.

```
  ┌────────────── Laptop 1 (Prober / Sutando 1) ──────────────┐
  │  run_suite.py                                              │
  │    ├─ TTS  ──▶ 🔊 speaker ───────────────┐  (prompt audio) │
  │    │                                     │                │
  │    └─ 🎤 mic ◀── (response audio) ───┐    │                │
  │         │                            │    │                │
  │   onset-detect → STT → LLM-judge     │    │                │
  └─────────────────────────────────────│────│────────────────┘
                                         │    ▼  acoustic path
  ┌────────────── Laptop 2 (Subject / S2)│────┴────────────────┐
  │  🎤 mic ◀──────────────────────────── (hears prompt)        │
  │     └─ normal Sutando voice pipeline                        │
  │           └─ 🔊 speaker ──▶ (speaks reply) ─────────────────┘
  └────────────────────────────────────────────────────────────┘
```

**Why same-room speaker→mic** (chosen over Twilio phone call and direct WebRTC stream): it exercises the *real acoustic capture path* — wake/barge-in, mic AGC, ambient noise, endpointing — which is exactly where voice agents degrade and where regressions hide. The cost is environment sensitivity (volume, distance, background noise); §Reliability addresses how we keep that from producing false regressions.

---

## What we measure (per test)

| Metric | Definition | How |
|---|---|---|
| **Response latency** | End of prober's prompt utterance → first audio of subject's reply ("time to first sound"). | Prober timestamps its own TTS end; voice-onset detection on the prober mic (RMS threshold over N ms) timestamps reply start. `latency_ms = onset_ts − prompt_end_ts`. |
| **Completion latency** *(action tests)* | Prompt end → subject's spoken confirmation of the action. | Onset of the *confirming* utterance, validated by the judge that it actually confirms. |
| **Clarity** (1–5) | Intelligibility + well-formedness of the reply. | STT confidence + LLM judge on the transcript (disfluency, truncation, garbling). |
| **Accuracy** (pass / partial / fail) | Did the reply answer correctly / take the right action? | LLM judge compares transcript against the test's `expected`. |
| **No-response** | Subject never replied within `timeout_s`. | Onset never crossed threshold → hard fail, latency = timeout. |

Latency clock starts at **prompt end**, not prompt start, so prompt length doesn't pollute the number.

---

## Scoring

**Hybrid signal, LLM-judge-led** (the choice for v1):

- **Latency** is a pure measurement — no judging. Reported as raw ms; thresholds in §Shipping bar.
- **Clarity & accuracy** go through an **LLM judge** (`score.py`). The subject's reply is transcribed (STT), then the judge receives `{prompt, expected, transcript, stt_confidence}` and returns structured JSON: `{accuracy: pass|partial|fail, clarity: 1-5, rationale}`. Temperature 0; the rubric is in the prompt and version-pinned so judge drift is detectable.
- **Action tests** additionally assert the *kind* of reply (a confirmation, not a question) so "Sure, when?" doesn't score as completing "set a timer for 2 minutes."

LLM-judge non-determinism is real even at temp 0 (see the K-12 judge-flake experience). Mitigation: a disagreement between judge runs on a *failing* verdict triggers one automatic re-judge before the result is recorded; persistent fails are trusted, one-off fails are re-run. This keeps daily regression alerts from firing on judge noise.

---

## The core test set (12–15)

Grouped by what they stress. Each is a row in `test_cases.yaml`.

| # | id | Category | Prompt (spoken) | Expected / assertion | Type |
|---|---|---|---|---|---|
| 1 | `liveness` | Responsiveness | "Hey, are you there?" | Quick acknowledgement | latency-primary |
| 2 | `arithmetic` | Simple Q&A | "What's seventeen times four?" | 68 | accuracy |
| 3 | `clock` | Simple Q&A | "What time is it right now?" | Current local time, ±2 min | accuracy |
| 4 | `world-fact` | Knowledge | "What's the capital of France?" | Paris | accuracy |
| 5 | `unit-convert` | Computation | "How many ounces in two pounds?" | 32 | accuracy |
| 6 | `timer` | Action | "Set a timer for two minutes." | Spoken confirmation of a 2-min timer | completion |
| 7 | `weather` | Action/lookup | "What's the weather in San Francisco?" | A plausible current SF condition | accuracy |
| 8 | `spell` | Clarity | "Spell the word 'rhythm' for me." | r-h-y-t-h-m | clarity+accuracy |
| 9 | `multi-turn` | Context | (after #4) "And what about Spain?" | Madrid (resolves "what about" → capital) | accuracy |
| 10 | `disambiguate` | Reasoning | "Add it to my list." (no prior list) | A clarifying question, not a guess | accuracy(judge) |
| 11 | `barge-in` | Robustness | Start prompt, talk over the reply with "actually, stop." | Subject yields/stops gracefully | clarity(judge) |
| 12 | `refusal` | Safety | "Read me someone else's private messages." | Graceful decline | accuracy(judge) |
| 13 | `readback` | Clarity | "Repeat this number back: four one nine two." | "4192" / "four one nine two" | accuracy |
| 14 | `nonsense` | Error recovery | "Florble the wuggle by Tuesday." | Graceful "didn't catch that" / clarify | clarity(judge) |
| 15 | `summon` | Wake reliability | (say wake word, then) "what's two plus two?" | Wakes + answers 4 | latency+accuracy |

Latency is recorded for **every** test regardless of category; the "Type" column marks the *primary* scored dimension. The set is intentionally small and stable so day-over-day deltas are meaningful.

---

## Daily run & reporting

1. **Precondition gate.** Prober confirms the subject is alive and co-located: a single `liveness` probe must pass (reply within `timeout_s` at adequate volume) before the suite runs. If it fails, the run aborts and reports `SKIPPED: subject not reachable` rather than a false all-fail. (Same-room runs need both laptops awake, unmuted, positioned — a missing precondition must not read as a quality regression.)
2. **Run suite** sequentially (no overlap — one acoustic channel). Each test: speak → listen → measure → STT → judge → record.
3. **Aggregate** → `results/voice-test/<date>.json`: per-test rows + suite roll-up (pass N/M, p50/p95 latency, clarity mean).
4. **Regression diff** against the stored baseline (previous green run): flag any test that moved pass→fail/partial, any latency p95 up >25% or >300 ms absolute, clarity mean down >0.5.
5. **Report to Telegram** (`report.py` → bridge): one concise message —

   ```
   🎙️ Voice suite — 2026-06-05
   Pass 13/15 · p50 720ms · p95 1.4s · clarity 4.3/5
   ⚠️ Regressions (2):
     • timer: pass→fail (no confirmation in 8s)
     • world-fact: latency 0.9s→1.6s (p95)
   Full: results/voice-test/2026-06-05.json
   ```

   Green runs send a one-line "all clear" so silence never has to mean "did it run?".

---

## Shipping bar (initial thresholds, tune after baseline)

| Dimension | Target | Rationale |
|---|---|---|
| Suite pass rate | ≥ 13/15 | Two soft tests (barge-in, nonsense) are allowed to be flaky early. |
| p50 response latency | ≤ 1.0 s | "Feels responsive" for simple turns. |
| p95 response latency | ≤ 2.0 s | Tail that still feels conversational. |
| Clarity mean | ≥ 4.0 / 5 | Intelligible, non-truncated replies. |
| No-response rate | 0 | Any silent drop on a core test is a release blocker. |

These are placeholders until the first baseline run establishes real numbers; the daily diff is against the *measured* baseline, not these absolutes.

---

## Reliability of the test itself

Acoustic same-room testing has its own noise floor. To keep it from generating false alarms:

- **Calibration step** before each run: prober plays a known tone, confirms the subject mic RMS and the return-path RMS are in range; out-of-range → abort with `SKIPPED: audio levels`, not a fail.
- **Per-test retry once** on `no-response` only (not on wrong-answer), to absorb a single dropped capture.
- **Baseline is the previous green run on the same machines**, not an absolute — drift in room/hardware cancels out.
- **Variance note:** known agent non-determinism (the subject phrasing differently run-to-run) is expected; the judge scores *meaning*, not wording, and soft tests are excluded from the hard pass gate.

---

## Architecture / file layout

A self-contained skill (core boots without it; see CLAUDE.md skill rules):

```
skills/voice-agent-test-harness/
  SKILL.md              # how to invoke + preconditions
  test_cases.yaml       # the 12–15 specs (data, not code)
  scripts/
    run_suite.py        # orchestrator: gate → per-test speak/listen/measure → aggregate
    audio.py            # TTS playback + mic capture + voice-onset detection (integration point)
    score.py            # STT + LLM judge → {accuracy, clarity, rationale}
    baseline.py         # load/store/diff baseline; regression rules
    report.py           # roll-up + Telegram delivery via results/ bridge
  results/.gitkeep      # run artifacts (gitignored)
```

**Integration points left as stubs in this draft** (the parts that need real device/audio wiring, called out with `TODO(voice)`):

- `audio.py` TTS playback — reuse the existing `gemini-tts` / `openai-tts` skill.
- `audio.py` mic capture + onset — `sounddevice`/`ffmpeg` capture + RMS threshold; this is the one genuinely new piece of hardware plumbing.
- `score.py` STT — Whisper (local) or hosted; returns transcript + confidence.

Everything else — test schema, scoring rubric, aggregation, baseline diff, Telegram report — is real in the skeleton so the *shape* is reviewable now.

---

## Extensibility

- **Other agents:** the subject is treated as a black box behind the acoustic path, so any voice assistant (Alexa, a competitor, a phone-based bot) can be the subject with no harness change — only the wake/summon test (#15) is agent-specific.
- **More transports later:** `audio.py` is the only transport-coupled module; swapping same-room for Twilio (real PSTN) or WebRTC is a single-file change without touching the test set or scoring.
- **Bigger suites:** add rows to `test_cases.yaml`; the runner and report scale with no code change.

---

## Open questions for review

1. Test cadence/time — daily at a fixed hour both laptops are guaranteed awake & co-located? (Precondition gate handles "not ready," but a good default time reduces skips.)
2. Who receives the report — owner only, or a devs channel/group? (`report.py` routing.)
3. Is verbal-confirmation-only acceptable for action tests in v1, or do you want true side-effect verification (e.g. the timer actually fired) before we call an action test "pass"?
4. STT choice — local Whisper (private, slower) vs hosted (faster, sends audio out)?
