#!/usr/bin/env python3
"""Regression pin: the test suite must never emit product telemetry.

A test that exercises a real code path without stubbing the network boundary
would POST to PostHog from CI or a dev box — the exact "a test was sending
metrics" incident this guards against. Every place that RUNS the suite opts
out via SUTANDO_TELEMETRY=0, and telemetry honors that opt-out. This test
fails if either half regresses:

  1. each test runner sets SUTANDO_TELEMETRY=0
     - scripts/coverage-gate.sh (local + coverage-gate.yml)
     - .github/workflows/ci.yml           (node + python tests)
     - .github/workflows/python39-compat.yml
  2. telemetry.opted_out() honors SUTANDO_TELEMETRY=0 (and enabled() → False)

Run: python3 tests/telemetry-test-optout-guard.test.py
"""
import importlib.util
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 1. every runner opts the suite out.
gate = (REPO / "scripts" / "coverage-gate.sh").read_text()
check("coverage-gate.sh exports SUTANDO_TELEMETRY=0",
      re.search(r"^\s*export\s+SUTANDO_TELEMETRY=0\b", gate, re.MULTILINE) is not None)

for wf in ("ci.yml", "python39-compat.yml"):
    text = (REPO / ".github" / "workflows" / wf).read_text()
    # a workflow-level `env:` mapping (top-level, not nested under a step) with
    # SUTANDO_TELEMETRY: "0" — applies to every job/step in the workflow.
    has = re.search(r'(?m)^env:\s*$', text) and re.search(
        r'(?m)^\s+SUTANDO_TELEMETRY:\s*["\']?0["\']?\s*$', text)
    check(f"{wf} sets SUTANDO_TELEMETRY=0", bool(has))


# 2. telemetry actually honors the opt-out (load the real module).
def _load_telemetry(env_value):
    prev = os.environ.get("SUTANDO_TELEMETRY")
    if env_value is None:
        os.environ.pop("SUTANDO_TELEMETRY", None)
    else:
        os.environ["SUTANDO_TELEMETRY"] = env_value
    # a key must be present, else _enabled() is already False for want of a key
    # and the test would pass vacuously — we want to prove the opt-out gates it.
    os.environ["POSTHOG_API_KEY"] = "phc_test"
    try:
        spec = importlib.util.spec_from_file_location("telemetry", REPO / "src" / "telemetry.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._KEY = "phc_test"
        return mod.opted_out(), mod.enabled()
    finally:
        if prev is None:
            os.environ.pop("SUTANDO_TELEMETRY", None)
        else:
            os.environ["SUTANDO_TELEMETRY"] = prev


opted, enab = _load_telemetry("0")
check("SUTANDO_TELEMETRY=0 → opted_out() True", opted is True)
check("SUTANDO_TELEMETRY=0 → enabled() False (no emission)", enab is False)

# control: with a key and no opt-out, telemetry WOULD emit — proving the opt-out
# above is what gates it, not a missing key.
opted_on, enab_on = _load_telemetry(None)
check("without opt-out (key present) → enabled() True (opt-out is the gate)",
      opted_on is False and enab_on is True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print(f"ALL PASS ({6} checks)")
