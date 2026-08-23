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

WORK = tempfile.mkdtemp(prefix="shepherd-durability-")
os.environ["SUTANDO_WORKSPACE"] = WORK

import shepherd_github as g  # noqa: E402
from shepherd_contract import Actor  # noqa: E402

g.state_dir = lambda: pathlib.Path(WORK) / "state" / "shepherd"

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


MINE = Actor(g.ACTOR_SCHEME, "qingyun0327@gmail.com")
PEER = Actor(g.ACTOR_SCHEME, "qingyun@ag2.ai")
scope = g.scope_for("o/r", 1, MINE)

# state written by THIS process...
path = g.save("task-dur-1", "o/r", 1, scope, "waiting", "initial")
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

# the write is atomic: no .tmp is left where a reader would find a partial file
check("no partial file left behind",
      sorted(p.suffix for p in g.state_dir().iterdir()), [".json"])

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
      raises(lambda: g.save("../../escaped", "o/r", 1, scope, "waiting")), True)
check("traversal id refused on load", raises(lambda: g.load("../../escaped")), True)
check("no file written outside the state dir", outside.exists(), False)
check("absolute path id refused", raises(lambda: g.load("/etc/passwd")), True)

# resume() must be monotonic: re-observing may not reopen a closed objective,
# and ordinary progress may not flatten blocked/needs_human into waiting
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.updated", g.subject_for(repo, num), MINE)

for prior in ("failed", "succeeded", "cancelled"):
    g.save("task-dur-2", "o/r", 1, scope, prior)
    state, why = g.resume("task-dur-2")
    check(f"terminal {prior} stays {prior}", state, prior)
    check(f"terminal {prior} not re-observed", "not re-observed" in why, True)

for prior in ("blocked", "needs_human", "waiting"):
    g.save("task-dur-2", "o/r", 1, scope, prior)
    state, _ = g.resume("task-dur-2")
    check(f"progress preserves {prior}", state, prior)

# an asserted actor's merge is surfaced but does not close the objective
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.merged", g.subject_for(repo, num), MINE)
g.save("task-dur-2", "o/r", 1, scope, "waiting")
state, why = g.resume("task-dur-2")
check("asserted merge does not terminate", state, "waiting")
check("but it is reported as proposed", "proposed=succeeded" in why, True)

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
print("PASS: 22 assertions, control verified")
