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
import os
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
    # VTH_CASES lets you point the runner at any case file (e.g. an exploratory
    # probe set) without touching the committed regression suite.
    path = Path(os.environ["VTH_CASES"]) if os.environ.get("VTH_CASES") else CASES_PATH
    return yaml.safe_load(path.read_text())


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


def _run_steps(case: dict, quick: bool = False) -> dict:
    """Multi-turn workflow test: speak each step in sequence within ONE voice
    session (so state carries across turns), judge each against its `expect`, and
    roll up. The final step is typically cleanup (delete the branch/doc) — tracked
    separately because 'did it tidy up after itself' is the real success signal."""
    import audio
    import score
    step_rows = []
    for i, step in enumerate(case["steps"]):
        say = step["say"]
        spoken = say if case.get("wake_word") else "Sutando, " + say
        # Continuous-capture (record across playback) so a fast reply that begins
        # the instant the prompt ends isn't clipped by capture-open latency.
        _, reply = audio.prompt_and_listen(spoken, step.get("timeout_s", 14))
        if reply.onset_at is None:
            step_rows.append({"step": i, "say": say, "accuracy": None,
                              "no_response": True, "transcript": ""})
            continue
        tr = score.transcribe(reply.wav_path)
        j = score.judge(say, step["expect"], tr, reply.wav_path)
        step_rows.append({"step": i, "say": say, "accuracy": j.accuracy,
                          "clarity": j.clarity, "rationale": j.rationale,
                          "transcript": tr.text})
    passed = sum(1 for s in step_rows if s.get("accuracy") == "pass")
    n = len(step_rows) or 1
    last_ok = bool(step_rows) and step_rows[-1].get("accuracy") == "pass"
    overall = "pass" if passed == len(step_rows) else ("partial" if passed >= (n + 1) // 2 else "fail")
    row = _row(case, None, overall, None,
               f"{passed}/{len(step_rows)} steps passed; cleanup {'ok' if last_ok else 'missed'}")
    row["steps"] = step_rows
    return row


def _run_one_live(case: dict, quick: bool = False) -> dict:
    import audio
    import score
    import time
    # Multi-turn workflow (PR flow, doc flow, …) — a sequence of dependent turns.
    if case.get("steps"):
        return _run_steps(case, quick)
    # Silence / false-wake test: the subject must NOT respond. With no prompt this
    # is the idle-silence test (don't speak unprompted over a long wait). With a
    # prompt it is a false-activation test: utter a line NOT addressed to the
    # subject (no wake word), then confirm it stays quiet. Pass = silent.
    if case.get("expect_silence"):
        window = min(float(case.get("timeout_s", 30)), 30.0) if quick \
            else float(case.get("timeout_s", 30))
        if case.get("prompt"):
            spoken = case["prompt"] if case.get("wake_word") else "Sutando, " + case["prompt"]
            # false-wake: continuous capture so a stray reply isn't missed either
            _, heard = audio.prompt_and_listen(spoken, window)
        else:
            heard = audio.listen(timeout_s=window)
        if heard.onset_at is None:
            msg = ("Correctly did not respond to un-addressed speech."
                   if case.get("prompt") else f"Stayed silent for ~{window:.0f}s (correct).")
            return _row(case, None, "pass", 5, msg)
        tr = score.transcribe(heard.wav_path)
        return _row(case, None, "fail", 1,
                    f"Responded when it should have stayed silent: '{tr.text[:60]}'",
                    transcript=tr.text)
    # Wake the subject every turn — a normal Sutando session needs the wake word
    # at the very start of each utterance (owner-confirmed 2026-06-05).
    spoken = case["prompt"] if case.get("wake_word") else "Sutando, " + case["prompt"]
    prompt, reply = audio.prompt_and_listen(spoken, case.get("timeout_s", 8))
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
    ap.add_argument("--confirm", type=int, default=0,
                    help="re-run each failing/no-response probe N more times; flags a "
                         "confirmed_defect only when it fails the majority (noise filter)")
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
        # Noise filter: re-run each suspicious (fail/partial/no-response) probe a
        # few more times. A real defect fails consistently; a room-noise fluke
        # passes on retry. confirmed_defect = failed the majority of attempts.
        if args.confirm:
            by_id = {t["id"]: t for t in tests}
            for row in rows:
                suspect = row.get("accuracy") in ("fail", "partial") or row.get("no_response")
                case = by_id.get(row.get("id"))
                if not (suspect and case):
                    continue
                first = "no_response" if row.get("no_response") else row.get("accuracy")
                outcomes = [first]
                for _ in range(args.confirm):
                    r2 = _safe_live(case, quick=args.quick)
                    outcomes.append("no_response" if r2.get("no_response") else r2.get("accuracy"))
                bad = sum(1 for o in outcomes if o in ("fail", "partial", "no_response", None))
                row["confirm_outcomes"] = outcomes
                row["confirmed_defect"] = bad > len(outcomes) / 2
                print(f"  [confirm] {row.get('id')}: {outcomes} -> "
                      f"{'CONFIRMED defect' if row['confirmed_defect'] else 'likely noise'}", flush=True)

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
