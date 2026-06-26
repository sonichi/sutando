"""Baseline storage + regression diff for the voice-agent test harness.

The baseline is the previous GREEN run on the same machines (not an absolute
threshold), so room/hardware drift cancels out. Regression rules live here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Regression thresholds (see docs/voice-agent-test-framework.md "Daily run").
LATENCY_P95_REL = 0.25      # flag if p95 latency up >25% ...
LATENCY_P95_ABS_MS = 300.0  # ... or >300ms absolute
CLARITY_DROP = 0.5          # flag if mean clarity down >0.5

_RANK = {"pass": 2, "partial": 1, "fail": 0}

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results" / "voice-test"
BASELINE_PATH = RESULTS_DIR / "baseline.json"


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def diff(current: dict, baseline: dict | None) -> list[str]:
    """Return human-readable regression lines. Empty list == all clear."""
    if baseline is None:
        return ["(no baseline yet — this run will seed it)"]

    regressions: list[str] = []
    base_tests = {t["id"]: t for t in baseline.get("tests", [])}

    for t in current.get("tests", []):
        b = base_tests.get(t["id"])
        if b is None:
            continue  # new test, nothing to compare
        # accuracy regression: rank dropped (pass->partial/fail, partial->fail)
        cur_rank = _RANK.get(t.get("accuracy", "fail"), 0)
        base_rank = _RANK.get(b.get("accuracy", "fail"), 0)
        if cur_rank < base_rank:
            regressions.append(
                f"{t['id']}: {b.get('accuracy')}→{t.get('accuracy')}"
                + (f" ({t.get('rationale')})" if t.get("rationale") else "")
            )

    # suite-level latency p95 + clarity
    cs, bs = current.get("summary", {}), baseline.get("summary", {})
    cp95, bp95 = cs.get("p95_latency_ms"), bs.get("p95_latency_ms")
    if cp95 is not None and bp95:
        if cp95 - bp95 > LATENCY_P95_ABS_MS and (cp95 - bp95) / bp95 > LATENCY_P95_REL:
            regressions.append(f"p95 latency {bp95:.0f}→{cp95:.0f}ms")
    cclar, bclar = cs.get("clarity_mean"), bs.get("clarity_mean")
    if cclar is not None and bclar is not None and bclar - cclar > CLARITY_DROP:
        regressions.append(f"clarity {bclar:.1f}→{cclar:.1f}")

    return regressions


def is_green(run: dict) -> bool:
    """A run is green (baseline-eligible) if no hard test failed and no soft
    test had a no-response. Soft-test wrong-answers don't block green."""
    for t in run.get("tests", []):
        if t.get("no_response"):
            return False
        if not t.get("soft") and t.get("accuracy") == "fail":
            return False
    return True


def promote(run_path: str) -> None:
    """Promote a run file to the regression baseline (only if green)."""
    run = load(Path(run_path))
    if run is None:
        raise SystemExit(f"no such run: {run_path}")
    if not is_green(run):
        raise SystemExit("run is not green — refusing to set a red baseline")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(run, indent=2))
    print(f"baseline ← {run_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", metavar="RUN_JSON", help="promote a green run to baseline")
    args = ap.parse_args()
    if args.promote:
        promote(args.promote)
    else:
        ap.print_help()
