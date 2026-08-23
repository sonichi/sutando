#!/usr/bin/env python3
"""The waiting contract must survive the process that created it."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Isolation comes from redirecting state_dir below, NOT from the environment:
# $SUTANDO_WORKSPACE stopped being honored in v0.8, so setting it isolates nothing.
WORK = tempfile.mkdtemp(prefix="shepherd-durability-")

import shepherd_github as g  # noqa: E402
from shepherd_contract import Actor  # noqa: E402

_REAL_STATE_DIR = g.state_dir
g.state_dir = lambda: pathlib.Path(WORK) / "state" / "shepherd"

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


MINE = Actor(g.ACTOR_SCHEME, "qingyun0327@gmail.com")
PEER = Actor(g.ACTOR_SCHEME, "qingyun@ag2.ai")
scope = g.scope_for("o/r", 1, MINE)

# state written by THIS process...
path = g.save("task-dur-1", scope, "waiting", "initial")
check("contract file exists", path.is_file(), True)

# ...is readable by a genuinely separate interpreter, given only the task id
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys,pathlib,json;"
     f"sys.path.insert(0,{str(ROOT / 'src')!r});"
     "import shepherd_github as g;"
     f"g.state_dir=lambda: pathlib.Path({WORK!r})/'state'/'shepherd';"
     "r=g.load('task-dur-1');"
     "print(json.dumps([r['repo'], r['number'], r['actor_value'], r['state']]))"],
    capture_output=True, text=True)
check("separate process reads the contract", probe.returncode, 0)
check("contract survives the writing process",
      json.loads(probe.stdout or "[]"), ["o/r", 1, "qingyun0327@gmail.com", "waiting"])

# a rehydrated scope must decide identically to the original
rehydrated = g.scope_from_saved(g.load("task-dur-1"))
check("rehydrated actor matches", rehydrated.actor.matches(MINE), True)
check("rehydrated rejects peer actor", rehydrated.actor.matches(PEER), False)
check("rehydrated keeps outcome conditions",
      rehydrated.success_conditions, scope.success_conditions)

# A .lock is not a partial file. Assert on temp files specifically, so a leaked
# .tmp still fails while a new intentional artifact does not.
check("no partial file left behind",
      sorted(p.name for p in g.state_dir().iterdir() if p.name.endswith(".tmp")), [])
check("the durable record is present",
      sorted(p.suffix for p in g.state_dir().iterdir() if p.suffix == ".json"), [".json"])

check("unknown task id is unknown, not a crash", g.load("task-does-not-exist"), None)


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


# a task_id becomes a filename, so an unvalidated one escapes the directory
outside = pathlib.Path(WORK) / "escaped.json"
check("traversal id refused on save",
      raises(lambda: g.save("../../escaped", scope, "waiting")), True)
check("traversal id refused on load", raises(lambda: g.load("../../escaped")), True)
check("no file written outside the state dir", outside.exists(), False)
check("absolute path id refused", raises(lambda: g.load("/etc/passwd")), True)

_REAL_OBSERVE = g.observe  # stubs below must not leak into later tests

# resume() must be monotonic: re-observing may not reopen a closed objective,
# and ordinary progress may not flatten blocked/needs_human into waiting
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.updated", g.subject_for(repo, num), MINE)

for prior in ("failed", "succeeded", "cancelled"):
    g.save("task-dur-2", scope, prior)
    state, why = g.resume("task-dur-2")
    check(f"terminal {prior} stays {prior}", state, prior)
    check(f"terminal {prior} not re-observed", "not re-observed" in why, True)

for prior in ("blocked", "needs_human", "waiting"):
    g.save("task-dur-2", scope, prior)
    state, _ = g.resume("task-dur-2")
    check(f"progress preserves {prior}", state, prior)

# an asserted actor's merge is surfaced but does not close the objective
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.merged", g.subject_for(repo, num), MINE)
g.save("task-dur-2", scope, "waiting")
state, why = g.resume("task-dur-2")
check("asserted merge does not terminate", state, "waiting")
check("but it is reported as proposed", "proposed=succeeded" in why, True)

g.observe = _REAL_OBSERVE  # restore: an unrestored stub makes later tests measure the stub

# --- the network seam itself: stub subprocess, exercise the real parsers ---
import subprocess as _sp  # noqa: E402

class _Res:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _with_gh(result, fn):
    real = _sp.run
    _sp.run = lambda *a, **k: result
    try:
        return fn()
    finally:
        _sp.run = real


# _gh surfaces a failed call instead of returning empty output that reads as "none"
check("_gh returns stdout on rc=0",
      _with_gh(_Res(0, "  hello \n"), lambda: g._gh("api", "x")), "hello")
try:
    _with_gh(_Res(1, "", "boom"), lambda: g._gh("api", "x"))
    check("_gh raises on rc!=0", "no raise", "RuntimeError")
except RuntimeError as e:
    check("_gh raises on rc!=0", "boom" in str(e), True)

# resolve_actor takes the LAST non-merge email: a branch's owner is whoever
# authored last, and gh already filtered merges out
check("resolve_actor takes the last line",
      _with_gh(_Res(0, "first@x\nsecond@x\nthird@x\n"),
               lambda: g.resolve_actor("o/r", 1)).value, "third@x")
check("resolve_actor uses the strong scheme",
      _with_gh(_Res(0, "a@x\n"), lambda: g.resolve_actor("o/r", 1)).scheme, g.ACTOR_SCHEME)
check("resolve_actor is None when nothing authored",
      _with_gh(_Res(0, "\n  \n"), lambda: g.resolve_actor("o/r", 1)), None)

# observe: a terminal state must win over ordinary progress
check("observe merged", _with_gh(_Res(0, "closed true"),
      lambda: g.observe("o/r", 1)).event_type, "github.pull_request.merged")
check("observe closed unmerged", _with_gh(_Res(0, "closed false"),
      lambda: g.observe("o/r", 1)).event_type, "github.pull_request.closed_unmerged")
check("observe open", _with_gh(_Res(0, "open false"),
      lambda: g.observe("o/r", 1)).event_type, "github.pull_request.updated")
check("observe carries a provider-native source id",
      _with_gh(_Res(0, "open false"), lambda: g.observe("o/r", 1)).source_id.startswith("o/r#1@"),
      True)

# Call the REAL state_dir: the stub above would only re-assert the stub.
import shepherd_github as _mod  # noqa: E402
_patched, _mod.state_dir = _mod.state_dir, _REAL_STATE_DIR
try:
    _real = str(_mod.state_dir())
finally:
    _mod.state_dir = _patched
# must sit under the resolved workspace, not the pre-v0.8 home tree
from workspace_default import resolve_workspace  # noqa: E402
check("real state_dir is under the resolved workspace",
      _real.startswith(str(resolve_workspace())), True)
check("real state_dir ends at state/shepherd", _real.endswith("/state/shepherd"), True)
check("real state_dir is not the deprecated home path", "/.sutando/" in _real, False)

# resume() paths not otherwise reached
check("resume on an unknown task id is 'unknown', not a crash",
      g.resume("task-not-persisted")[0], "unknown")

# an event that is not admitted preserves state and says why
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.updated", g.subject_for(repo, num), PEER)
g.save("task-dur-3", scope, "waiting")
_st, _why = g.resume("task-dur-3")
check("unadmitted event preserves state", _st, "waiting")
check("unadmitted event reports the decision", "ignored" in _why, True)

# a VERIFIED actor's outcome does terminate, exercising the terminal branch
VERIFIED = Actor("matrix.mxid", "@qingyun-air.agent:ag2.space")
vscope = g.scope_for("o/r", 2, VERIFIED)
g.save("task-dur-4", vscope, "waiting")
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.merged", g.subject_for(repo, num), VERIFIED)
check("verified merge terminates the objective", g.resume("task-dur-4")[0], "succeeded")
g.observe = _REAL_OBSERVE

# negative control: the harness must be able to register a failure
_n = len(failures)
check("CONTROL (expected to fail)", 1, 2)
if len(failures) != _n + 1:
    print("FAIL: control undetected — this harness cannot fail")
    sys.exit(1)
failures.pop()

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: 39 assertions, control verified")
