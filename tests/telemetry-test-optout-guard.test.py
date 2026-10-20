#!/usr/bin/env python3
"""Regression pin: the test suite must never emit product telemetry.

A test that exercises a real code path without stubbing the network boundary
would POST to PostHog from CI or a dev box — the exact "a test was sending
metrics" incident this guards against. Every place that RUNS the suite opts
out via SUTANDO_TELEMETRY=0, and telemetry honors that opt-out. This test
fails if either half regresses:

  1. each test runner sets SUTANDO_TELEMETRY=0
     - scripts/coverage-gate.sh (local; ci.yml's coverage-gate job)
     - .github/workflows/ci.yml           (node + python tests)
     - .github/workflows/python39-compat.yml
     - .github/workflows/windows-runtime.yml
  2. telemetry.opted_out() honors SUTANDO_TELEMETRY=0 (and enabled() → False)
  3. tests/_helpers/hermetic_env.py, which rebuilds a test's environment, keeps it off

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

for wf in ("ci.yml", "python39-compat.yml", "windows-runtime.yml"):
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

# 3. the shared test environment carries the opt-out itself: it is rebuilt from an allowlist,
# so a runner-level SUTANDO_TELEMETRY=0 does not reach a test that uses it.
import sys
from unittest import mock
sys.path.insert(0, str(REPO / "tests"))
from _helpers.hermetic_env import hermetic_env  # noqa: E402

with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "POSTHOG_API_KEY": "phc_test"}, clear=True):
    built = hermetic_env()
check("hermetic_env() sets SUTANDO_TELEMETRY=0 with no opt-out outside", built.get("SUTANDO_TELEMETRY") == "0")
check("hermetic_env() sets DO_NOT_TRACK=1 with no opt-out outside", built.get("DO_NOT_TRACK") == "1")
with mock.patch.dict(os.environ, {**built, "POSTHOG_API_KEY": "phc_test"}, clear=True):
    spec = importlib.util.spec_from_file_location("telemetry_hermetic", REPO / "src" / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._KEY = "phc_test"
    check("telemetry.enabled() is False inside hermetic_env()", mod.enabled() is False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print(f"ALL PASS ({10} checks)")
