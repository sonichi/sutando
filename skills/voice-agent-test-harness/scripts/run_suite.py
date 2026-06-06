"""Voice-agent test harness — orchestrator (prober side).

Flow per docs/voice-agent-test-framework.md:
  precondition gate (calibrate + liveness) -> for each test: speak -> listen ->
  measure latency -> STT -> LLM judge -> record -> aggregate -> diff baseline ->
  report to Telegram.

--dry-run exercises schema/scoring/aggregation/reporting with canned transcripts
(no audio, no model calls) so the whole pipeline is runnable for review today.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import baseline as bl     # noqa: E402
import report as rp       # noqa: E402

try:
    import yaml           # noqa: E402
except ImportError:
    yaml = None

CASES_PATH = HERE.parent / "test_cases.yaml"


def load_cases() -> dict:
    if yaml is None:
        raise SystemExit("pyyaml required: pip install pyyaml")
    return yaml.safe_load(CASES_PATH.read_text())


# ---- canned data for --dry-run (illustrative, not real measurements) ----
_CANNED = {
    "liveness":    ("Yes, I'm here.", 0.97, 540.0),
    "arithmetic":  ("That's sixty-eight.", 0.96, 690.0),
    "clock":       ("It's about 10:22 PM.", 0.95, 720.0),
    "world-fact":  ("The capital of France is Paris.", 0.98, 610.0),
    "unit-convert": ("Two pounds is thirty-two ounces.", 0.96, 740.0),
    "timer":       ("Done — a two minute timer is set.", 0.97, 880.0),
    "weather":     ("It's around 60 degrees and foggy in San Francisco.", 0.93, 1180.0),
    "spell":       ("Sure: r, h, y, t, h, m.", 0.94, 900.0),
    "multi-turn":  ("The capital of Spain is Madrid.", 0.97, 680.0),
    "disambiguate": ("Which list would you like me to add it to?", 0.95, 820.0),
    "barge-in":    ("Okay, stopping.", 0.9, 950.0),
    "refusal":     ("I can't read other people's private messages.", 0.96, 770.0),
    "readback":    ("Four one nine two.", 0.95, 700.0),
    "nonsense":    ("Sorry, I didn't quite catch that — could you rephrase?", 0.88, 990.0),
    "summon":      ("Four.", 0.96, 1020.0),
}


def _safe_live(case: dict, quick: bool = False) -> dict:
    """Never let one test's exception (network timeout, etc.) kill the whole
    suite — record it as a failed test and keep going."""
    try:
        return _run_one_live(case, quick=quick)
    except Exception as e:
        return _row(case, None, "fail", None,
                    f"runner error: {type(e).__name__}", no_response=True)


def _run_one_live(case: dict, quick: bool = False) -> dict:
    import audio
    import score
    import time
    # Wake the subject every turn — a normal Sutando session needs the wake word
    # at the very start of each utterance (owner-confirmed 2026-06-05).
    spoken = case["prompt"] if case.get("wake_word") else "Sutando, " + case["prompt"]
    prompt = audio.speak(spoken)
    reply = audio.listen(timeout_s=case.get("timeout_s", 8))
    lat = audio.latency_ms(prompt, reply)
    tr = score.transcribe(reply.wav_path)
    if reply.onset_at is None:
        return _row(case, lat, None, None, "", no_response=True)
    j = score.judge(case["prompt"], case["expected"], tr, reply.wav_path)
    row = _row(case, lat, j.accuracy, j.clarity, j.rationale, transcript=tr.text)

    # Real side-effect verification for action tests (e.g. the timer actually fires).
    effect = case.get("effect")
    if effect and j.accuracy != "fail":
        fire_after = 30 if quick else float(effect.get("fire_after_s", 30))
        window = float(effect.get("listen_window_s", 15))
        # wait until shortly before the effect is due, then listen through it.
        time.sleep(max(0, fire_after - 2))
        fired = audio.listen(timeout_s=window + 2)
        ftr = score.transcribe(fired.wav_path)
        if fired.onset_at is None:
            row["effect_verified"] = False
            row["effect_note"] = "no sound at expected fire time"
            row["accuracy"] = "partial"   # confirmed verbally but effect not observed
        else:
            fj = score.judge("Did the timer fire?", effect["expected"], ftr)
            row["effect_verified"] = (fj.accuracy == "pass")
            row["effect_note"] = ftr.text
            if not row["effect_verified"]:
                row["accuracy"] = "partial"
    return row


def _run_one_dry(case: dict) -> dict:
    text, conf, lat = _CANNED.get(case["id"], ("(no canned reply)", 0.5, 1500.0))
    # In dry-run we skip the model and assume the canned reply is correct, with
    # clarity derived from STT confidence — enough to exercise aggregation/diff.
    clarity = max(1, min(5, round(conf * 5)))
    return _row(case, lat, "pass", clarity, "dry-run canned")


def _row(case, lat, accuracy, clarity, rationale, no_response=False, transcript="") -> dict:
    return {
        "id": case["id"],
        "category": case.get("category"),
        "soft": bool(case.get("soft")),
        "latency_ms": lat,
        "accuracy": accuracy,
        "clarity": clarity,
        "rationale": rationale,
        "transcript": transcript,
        "no_response": no_response,
    }


def precondition_gate(dry: bool) -> tuple[bool, str]:
    if dry:
        return True, "dry-run: gate skipped"
    import audio
    ok, reason = audio.calibrate()
    if not ok:
        return False, f"audio levels: {reason}"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no audio/model; canned data")
    ap.add_argument("--only", help="run a single test id")
    ap.add_argument("--deliver", action="store_true", help="send the owner report")
    ap.add_argument("--quick", action="store_true", help="shorten long effect waits to 30s")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    args = ap.parse_args()

    cfg = load_cases()
    tests = cfg["tests"]
    if args.only:
        tests = [t for t in tests if t["id"] == args.only]
        if not tests:
            raise SystemExit(f"no test id: {args.only}")

    ok, reason = precondition_gate(args.dry_run)
    if not ok:
        print(f"SKIPPED: {reason}")
        if args.deliver:
            rp.deliver(f"🎙️ Voice suite — {args.date}\nSKIPPED: {reason}")
        return 0

    if args.dry_run:
        rows = [_run_one_dry(t) for t in tests]
    else:
        rows = [_safe_live(t, quick=args.quick) for t in tests]

    run = {
        "suite": cfg.get("suite"),
        "version": cfg.get("version"),
        "date": args.date,
        "dry_run": args.dry_run,
        "tests": rows,
    }
    run["summary"] = rp.summarize(rows)

    out_dir = bl.RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.date}.json"
    out_path.write_text(json.dumps(run, indent=2))

    regressions = bl.diff(run, bl.load(bl.BASELINE_PATH))
    msg = rp.render(run, regressions, args.date)
    print(msg)
    print(f"\nwrote {out_path}")
    if args.deliver:
        print("→", rp.deliver(msg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
