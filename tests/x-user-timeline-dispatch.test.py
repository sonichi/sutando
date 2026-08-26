#!/usr/bin/env python3
"""`user-timeline` dispatch: hermetic, and asserts the CALL rather than the absence of an error.

Rewritten after review. The first version ran the CLI as a subprocess and treated
"did not print the limit error" as acceptance — which a DNS failure, an HTTP
error, a traceback or a silent return all satisfy. It proved nothing about
dispatch or argument forwarding, and its accepted cases entered the real X
request path, where the subprocess could reload the repo .env and use live
credentials.

This version imports the module once, replaces `get_user_timeline` and
`get_auth` with recorders, and asserts what was invoked and with which
arguments. No network, no subprocess, no .env influence on the outcome.

Pins three behaviours:
  * 5 and 100 reach get_user_timeline exactly once, with the parsed arguments.
  * 4 and 101 reach neither it nor get_auth() — refused on the bound alone.
  * No bearer reaches neither, and says X_BEARER_TOKEN rather than reporting a
    pip problem: get_auth() loads the optional OAuth deps, so ordering the
    bearer check after it misreports a clean host.
"""

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "x-twitter" / "x-post.py"
failures = []
checked = 0


def check(label, ok, detail=""):
    global checked
    checked += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if not ok else ""))
    if not ok:
        failures.append(label)


# Import an ISOLATED COPY, never the repository script. x-post.py reads
# `<script>/../../.env` at import and writes its values into os.environ.
_ISO = pathlib.Path(tempfile.mkdtemp(prefix="xp-iso-")) / "skills" / "x-twitter"
_ISO.mkdir(parents=True)
shutil.copy2(SCRIPT, _ISO / SCRIPT.name)
ISO_SCRIPT = _ISO / SCRIPT.name


def load():
    """Fresh module each time so module-level globals cannot leak between cases."""
    spec = importlib.util.spec_from_file_location("xp_under_test", ISO_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(argv, bearer):
    """Invoke main() with recorders in place. Returns (exit_code, calls, output)."""
    mod = load()
    mod.BEARER_TOKEN = bearer          # after import: .env cannot influence the case
    calls = {"timeline": [], "auth": 0}
    mod.get_user_timeline = lambda *a, **k: calls["timeline"].append((a, k))

    def _auth(*_a, **_k):
        # RECORD, never raise: raising here aborts the whole suite on the first
        # offending case, so one defect hides every check after it.
        calls["auth"] += 1
        raise SystemExit(90)   # sentinel exit, distinguishable from the CLI's own codes

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
    check(f"--limit {limit}: get_auth not reached", calls["auth"] == 0)

# --exclude must reach the function, not be silently dropped.
code, calls, out = run(
    ["user-timeline", "Chi_Wang_", "--limit", "5", "--exclude", "retweets,replies"], "tok")
check("--exclude forwarded verbatim",
      bool(calls["timeline"]) and calls["timeline"][0][1].get("exclude") == "retweets,replies",
      f"calls={calls['timeline']}")

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

# --- the import itself must not touch host config -------------------------
before = dict(os.environ)
load()
check("importing the module does not mutate os.environ",
      dict(os.environ) == before,
      f"changed keys: {sorted(set(os.environ) ^ set(before)) or 'values differ'}")
check("the imported copy is not the repository script", ISO_SCRIPT != SCRIPT)
check("the isolated copy sees no .env",
      not (ISO_SCRIPT.parent.parent.parent / ".env").exists(),
      str(ISO_SCRIPT.parent.parent.parent / ".env"))

# --- completion is pinned: a case dropped or an early abort fails here -----
EXPECTED = 24
check(f"all {EXPECTED} checks ran (guards against a silent early exit)",
      checked + 1 == EXPECTED, f"ran {checked + 1}")

print(f"\n{checked - len(failures)}/{checked} passed")
sys.exit(1 if failures else 0)
