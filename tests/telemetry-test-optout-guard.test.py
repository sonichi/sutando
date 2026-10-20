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
  4. a child process whose environment a test rebuilt by hand (env={"PATH": ..., "HOME": tmp})
     carries no opt-out variable, so src/telemetry.py refuses to emit when the install id
     would live under a fixed system temp location (ephemeral_identity), while a persistent
     HOME stays counted even when TMPDIR points at it

IMPORTANT: every one of these is load-bearing. A test run that emits counts as a real user on
the admin DAU, and each run mints a fresh install id, so a single CI run can add hundreds of
"users". Do not remove, weaken or bypass the opt-out in the runners, in hermetic_env, or in
telemetry.ephemeral_identity() to make a test pass; stub the network boundary instead.

Run: python3 tests/telemetry-test-optout-guard.test.py
"""
import importlib.util
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures = []


checked = []


def check(name, cond, detail=""):
    checked.append(name)
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

import subprocess  # noqa: E402
import tempfile  # noqa: E402

# 4. a child whose environment a test rebuilt from nothing (env={"PATH": ..., "HOME": <temp dir>})
# inherits no opt-out variable at all; telemetry itself must still refuse to emit there.

PROBE = ("import sys; sys.path.insert(0, sys.argv[1]); import telemetry; "
         "telemetry._KEY = 'phc_test'; print(telemetry.enabled())")


def _child_enabled(home):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "POSTHOG_API_KEY": "phc_test",
           "SUTANDO_STATE_DIR": os.path.join(home, "state")}
    out = subprocess.run([sys.executable, "-c", PROBE, str(REPO / "src")], env=env,
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip() or out.stderr.strip()


with tempfile.TemporaryDirectory() as fresh_home:
    got = _child_enabled(fresh_home)
check("child with a rebuilt env and a temp HOME → enabled() False", got == "False", got)
if sys.platform == "darwin":  # the per-user temp macOS assigns, whatever TMPDIR says
    user_temp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(dir=user_temp) as fresh_home:
        got = _child_enabled(fresh_home)
    check("child with a rebuilt env and HOME under DARWIN_USER_TEMP_DIR → enabled() False", got == "False", got)
# the same decision in this process, where the coverage gate can see it.
with tempfile.TemporaryDirectory() as fresh_home, mock.patch.dict(
        os.environ, {"HOME": fresh_home, "POSTHOG_API_KEY": "phc_test"}, clear=False):
    os.environ.pop("SUTANDO_TELEMETRY_ID_FILE", None)
    os.environ.pop("XDG_DATA_HOME", None)
    spec = importlib.util.spec_from_file_location("telemetry_temp_home", REPO / "src" / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("in-process: a temp HOME → ephemeral_identity() True", mod.ephemeral_identity() is True)
    with mock.patch.dict(os.environ, {"SUTANDO_STATE_DIR": os.path.join(fresh_home, "state")}):
        os.environ.pop("SUTANDO_TELEMETRY", None)
        os.environ.pop("DO_NOT_TRACK", None)
        check("in-process: a temp HOME with no opt-out variable → opted_out() True", mod.opted_out() is True)
    folders = os.path.join(os.sep, "fake-var-folders")
    check("the macOS per-user temp <folders>/<x>/<y>/T counts, its siblings do not",
          mod._macos_user_temp(os.path.join(folders, "ab", "cd", "T", "tmp1"), folders)
          and not mod._macos_user_temp(os.path.join(folders, "ab", "cd", "C", "x"), folders)
          and not mod._macos_user_temp(os.path.join(folders, "ab"), folders))
    win = {"LOCALAPPDATA": "C:\\Users\\u\\AppData\\Local", "SYSTEMROOT": "C:\\Windows", "TEMP": "D:\\custom"}
    check("Windows roots are LOCALAPPDATA\\Temp and SystemRoot\\Temp, never TEMP",
          mod._fixed_temp_roots(nt=True, env=win) == ["C:\\Users\\u\\AppData\\Local\\Temp", "C:\\Windows\\Temp"])
    os.environ["SUTANDO_TELEMETRY"] = "1"
    check("in-process: an explicit SUTANDO_TELEMETRY=1 wins over a temp HOME", mod.ephemeral_identity() is False)
    os.environ.pop("SUTANDO_TELEMETRY")
    os.environ["SUTANDO_TELEMETRY_ID_FILE"] = os.path.join(fresh_home, "telemetry-id")
    check("in-process: an explicit SUTANDO_TELEMETRY_ID_FILE is kept", mod.ephemeral_identity() is False)

# TMPDIR (or the cwd fallback) can name a persistent HOME; only fixed system temp locations count.
PERSISTENT_HOME = "/nonexistent-sutando-telemetry-guard-home"
with mock.patch.dict(os.environ, {"HOME": PERSISTENT_HOME, "TMPDIR": PERSISTENT_HOME,
                                  "POSTHOG_API_KEY": "phc_test", "SUTANDO_STATE_DIR": PERSISTENT_HOME + "/state"}), \
        mock.patch.object(tempfile, "tempdir", PERSISTENT_HOME):
    for k in ("SUTANDO_TELEMETRY_ID_FILE", "XDG_DATA_HOME", "SUTANDO_TELEMETRY", "DO_NOT_TRACK"):
        os.environ.pop(k, None)
    spec = importlib.util.spec_from_file_location("telemetry_tmpdir_home", REPO / "src" / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._KEY = "phc_test"
    check("TMPDIR set to a persistent HOME → still enabled()", tempfile.gettempdir() == PERSISTENT_HOME
          and mod.ephemeral_identity() is False and mod.enabled() is True)
# A home that cannot be determined (Windows: no USERPROFILE/HOMEPATH makes Path.home() raise) or
# is relative (resolved against the cwd) gives no stable id either.
with mock.patch.dict(os.environ, {"POSTHOG_API_KEY": "phc_test"}):
    for k in ("SUTANDO_TELEMETRY_ID_FILE", "XDG_DATA_HOME", "SUTANDO_TELEMETRY"):
        os.environ.pop(k, None)
    spec = importlib.util.spec_from_file_location("telemetry_no_home", REPO / "src" / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _no_home():
        raise RuntimeError("Could not determine home directory.")

    with mock.patch.object(mod.Path, "home", _no_home):
        check("an undeterminable home (Windows rebuilt env) → ephemeral_identity() True",
              mod.ephemeral_identity() is True)
        os.environ["SUTANDO_TELEMETRY"] = "1"
        check("an undeterminable home with SUTANDO_TELEMETRY=1 → the opt-in wins", mod.ephemeral_identity() is False)
        os.environ.pop("SUTANDO_TELEMETRY")
    cwd = os.getcwd()
    try:
        os.chdir(os.path.abspath(os.sep))
        with mock.patch.object(mod.Path, "home", lambda: Path("relative-home")):
            check("a relative home → ephemeral_identity() True", mod.ephemeral_identity() is True)
    finally:
        os.chdir(cwd)

# control: the same rebuilt env with a HOME outside any temp dir does emit, so the temp HOME is the gate.
got = _child_enabled("/nonexistent-sutando-telemetry-guard-home")
check("control: rebuilt env with a non-temp HOME → enabled() True", got == "True", got)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print(f"ALL PASS ({len(checked)} checks)")
