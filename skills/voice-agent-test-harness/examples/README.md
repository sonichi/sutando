# Example run-log — live voice session

`run-2026-06-06.json` is a **real** harness run against a live voice session
(`dry_run: false`) — not canned. It is committed as merge evidence that the
pipeline executes end-to-end (precondition gate → speak → capture → STT →
LLM judge → score), per the live-result request on PR #1469.

Day-to-day run-logs are gitignored (`results/voice-test/`); this one copy is
pinned under `examples/` as a stable reference. Re-generate with:

```bash
python3 scripts/run_suite.py            # writes results/voice-test/<date>.json
```

## What this run shows (suite `core-v1`, 26 turns, real audio)

- **p50 latency 1376 ms**, p95 4746 ms; clarity mean 4.75/5 on captured turns.
- **Captured-turn defects reproduce** the behavior circulating in #dev:
  - `arithmetic` → `"Working on it"` (deferred, no answer) — latency 1324 ms
  - `unit-convert` → `"working on it"` (deferred) — 1178 ms
  - `version` → `"Welcome to"` (garbled) — 1845 ms
  - `false-wake` → replied to un-addressed speech, clarity 1
- **Passes** are real captured replies: `liveness`, `disambiguate`, `barge-in`,
  `refusal`, `readback` (`"the number is 4192"`), `dual-request`
  (`"…Tokyo and 10 + 15 is 25"`), `confirm-before-action`, `silence`.
- `no_response: 10` — the endurance cluster (turns deepest in the session).
  This is an energy-onset decision made **before** transcription; the exact
  count is still being de-confounded from test-ordering depth (see PR notes).

Each turn carries `latency_ms`, `accuracy`, `clarity`, `transcript`, and the
judge `rationale`, so a reader can audit any verdict against what was actually
heard.
