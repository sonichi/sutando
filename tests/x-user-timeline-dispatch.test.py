#!/usr/bin/env python3
"""`user-timeline` dispatch: hermetic, and asserts the CALL rather than the absence of an error.

Rewritten after review. The first version ran the CLI as a subprocess and treated
"did not print the limit error" as acceptance — which a DNS failure, an HTTP
error, a traceback or a silent return all satisfy. It proved nothing about
dispatch or argument forwarding, and its accepted cases entered the real X
request path, where the subprocess could reload the repo .env and use live
credentials.

This version loads a fresh copy of the module per case, replaces
`get_user_timeline` and `get_auth` with recorders, and asserts what was invoked,
with which arguments, and with which exit status. No network and no .env
influence on the outcome; the single subprocess is the temp-cleanup probe,
which must observe a run that has already exited.

Pins:
  * 5 and 100 reach get_user_timeline exactly once, with the parsed arguments,
    and the command exits 0.
  * The DEFAULT --limit reaches it as max_results=10 with exclude=None.
  * 4 and 101 reach neither it nor get_auth() — refused on the bound alone.
  * No bearer reaches neither, and says X_BEARER_TOKEN rather than reporting a
    pip problem: get_auth() loads the optional OAuth deps, so ordering the
    bearer check after it misreports a clean host.
  * The module under test is the isolated copy, asserted by its resolved
    __file__, and importing it does not mutate os.environ.

Every assertion here was checked to FAIL against a deliberate mutation; a check
that passes in both worlds is not a test.
"""

import atexit
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "x-twitter" / "x-post.py"
failures = []
checked = 0

# Captured BEFORE the first import: a snapshot taken after a load compares a
# mutated environ with itself, so an idempotent import-time write would pass.
_ENV_BEFORE = dict(os.environ)


def check(label, ok, detail=""):
    global checked
    checked += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if not ok else ""))
    if not ok:
        failures.append(label)


# Load an ISOLATED COPY, never the repository script. x-post.py reads
# `<script>/../../.env` at import and writes its values into os.environ.
_TMP = tempfile.TemporaryDirectory(prefix="xp-iso-")
atexit.register(_TMP.cleanup)
_ISO = pathlib.Path(_TMP.name) / "skills" / "x-twitter"
_ISO.mkdir(parents=True)
shutil.copy2(SCRIPT, _ISO / SCRIPT.name)
ISO_SCRIPT = _ISO / SCRIPT.name


def load():
    """Import a FRESH copy per call, so module-level globals cannot leak between cases."""
    spec = importlib.util.spec_from_file_location("xp_under_test", ISO_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the import itself must be isolated and inert -------------------------
# Runs FIRST: _ENV_BEFORE is only meaningful against the very first import.
_first = load()
check("the module under test resolves to the isolated copy, not the repo script",
      pathlib.Path(_first.__file__).resolve() == ISO_SCRIPT.resolve(),
      f"__file__={_first.__file__}")
check("the isolated copy sees no .env",
      not (ISO_SCRIPT.parent.parent.parent / ".env").exists(),
      str(ISO_SCRIPT.parent.parent.parent / ".env"))
check("the first import does not mutate os.environ",
      dict(os.environ) == _ENV_BEFORE,
      f"changed keys: {sorted(set(os.environ) ^ set(_ENV_BEFORE)) or 'values differ'}")


def run(argv, bearer):
    """Invoke main() with recorders in place. Returns (exit_code, calls, output)."""
    mod = load()
    mod.BEARER_TOKEN = bearer          # after import: .env cannot influence the case
    calls = {"timeline": [], "auth": 0}
    mod.get_user_timeline = lambda *a, **k: calls["timeline"].append((a, k))

    def _auth(*_a, **_k):
        # Sentinel exit, caught by run(): an unexpected get_auth() marks its own
        # case instead of aborting the suite and hiding every check after it.
        calls["auth"] += 1
        raise SystemExit(90)

    mod.get_auth = _auth
    buf = io.StringIO()
    code = 0
    old = sys.argv
    sys.argv = ["x-post.py"] + argv
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                mod.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old
    return code, calls, buf.getvalue()


# --- accepted bounds actually DISPATCH, with the arguments parsed ------------
for limit in (5, 100):
    code, calls, out = run(["user-timeline", "Chi_Wang_", "--limit", str(limit)], "tok")
    check(f"--limit {limit}: get_user_timeline called exactly once",
          len(calls["timeline"]) == 1, f"called {len(calls['timeline'])}x, out={out[:60]!r}")
    if calls["timeline"]:
        a, k = calls["timeline"][0]
        check(f"--limit {limit}: forwarded username and limit",
              a[:1] == ("Chi_Wang_",) and k.get("max_results") == limit,
              f"args={a} kwargs={k}")
    # An accepted run must also SUCCEED. Without this, a stray non-zero exit
    # after a correct dispatch is invisible.
    check(f"--limit {limit}: exits 0", code == 0, f"rc={code}, out={out[:60]!r}")
    check(f"--limit {limit}: get_auth not reached", calls["auth"] == 0)

# --exclude must reach the function, not be silently dropped.
code, calls, out = run(
    ["user-timeline", "Chi_Wang_", "--limit", "5", "--exclude", "retweets,replies"], "tok")
check("--exclude: dispatched exactly once",
      len(calls["timeline"]) == 1, f"called {len(calls['timeline'])}x")
check("--exclude forwarded verbatim",
      len(calls["timeline"]) == 1 and calls["timeline"][0][1].get("exclude") == "retweets,replies",
      f"calls={calls['timeline']}")
if calls["timeline"]:
    _a, _k = calls["timeline"][0]
    check("--exclude call still carries username and limit",
          _a[:1] == ("Chi_Wang_",) and _k.get("max_results") == 5, f"args={_a} kwargs={_k}")
check("--exclude case exits 0", code == 0, f"rc={code}, out={out[:60]!r}")

# --- the DEFAULT limit is part of the contract ------------------------------
# Without it the default could drift to a bound-rejected value unnoticed.
code, calls, out = run(["user-timeline", "Chi_Wang_"], "tok")
check("no --limit: exits 0", code == 0, f"rc={code}, out={out[:70]!r}")
check("no --limit: get_user_timeline called exactly once",
      len(calls["timeline"]) == 1, f"called {len(calls['timeline'])}x, out={out[:60]!r}")
if calls["timeline"]:
    a, k = calls["timeline"][0]
    check("no --limit: defaults to max_results=10", k.get("max_results") == 10, f"kwargs={k}")
    check("no --limit: defaults to exclude=None", k.get("exclude") is None, f"kwargs={k}")
check("no --limit: get_auth not reached", calls["auth"] == 0)

# --- rejected bounds dispatch to NOTHING ------------------------------------
for limit in (4, 101):
    code, calls, out = run(["user-timeline", "Chi_Wang_", "--limit", str(limit)], "tok")
    check(f"--limit {limit}: refused with rc=2", code == 2, f"rc={code}")
    check(f"--limit {limit}: get_user_timeline NOT called", not calls["timeline"])
    check(f"--limit {limit}: get_auth NOT called", calls["auth"] == 0)
    check(f"--limit {limit}: message names 5..100", "between 5 and 100" in out, out[:70])

# --- no bearer: refused before get_auth(), and says which credential ---------
code, calls, out = run(["user-timeline", "Chi_Wang_", "--limit", "10"], "")
check("no bearer: rc=2", code == 2, f"rc={code}")
check("no bearer: get_user_timeline NOT called", not calls["timeline"])
check("no bearer: get_auth NOT reached (would report a pip problem)", calls["auth"] == 0)
check("no bearer: names X_BEARER_TOKEN", "X_BEARER_TOKEN" in out, out[:80])
check("no bearer: does not report a dependency problem",
      "pip3 install" not in out and "missing dependencies" not in out, out[:80])

# --- temp cleanup is pinned by RUNNING THIS SUITE as a subprocess and looking
# for what it left behind. An in-process check cannot see atexit cleanup at all.
if not os.environ.get("XP_SKIP_TMP_PROBE"):
    _tmproot = pathlib.Path(tempfile.gettempdir())
    _before = {q.name for q in _tmproot.glob("xp-iso-*")}
    _child = subprocess.run([sys.executable, __file__],
                            env={**os.environ, "XP_SKIP_TMP_PROBE": "1"},
                            capture_output=True, text=True)
    _after = {q.name for q in _tmproot.glob("xp-iso-*")}
    # rc AND the tail handshake: a child that died before creating its tree also
    # leaves nothing behind, so absence alone passes for the wrong reason.
    check("cleanup probe: the child ran to completion",
          _child.returncode == 0 and "/" in _child.stdout.strip().splitlines()[-1],
          f"rc={_child.returncode} tail={_child.stdout.strip().splitlines()[-1:]!r}")
    check("a completed run of this suite leaves no xp-iso-* tree",
          not (_after - _before), f"leaked: {sorted(_after - _before)}")
else:
    check("cleanup probe: the child ran to completion (inner run: skipped)", True)
    check("a completed run of this suite leaves no xp-iso-* tree (inner run: skipped)", True)


# --- completion is pinned: a case dropped or an early abort fails here -----
EXPECTED = 36
check(f"all {EXPECTED} checks ran (guards against a silent early exit)",
      checked + 1 == EXPECTED, f"ran {checked + 1}")

print(f"\n{checked - len(failures)}/{checked} passed")
sys.exit(1 if failures else 0)
