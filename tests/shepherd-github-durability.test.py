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
# The GitHub adapter lives at the optional skill edge, not in core.
SKILL = ROOT / "skills" / "pr-shepherd-contract" / "scripts"
sys.path.insert(0, str(SKILL))

# Isolation comes from redirecting state_dir below, NOT from the environment:
# $SUTANDO_WORKSPACE stopped being honored in v0.8, so setting it isolates nothing.
WORK = tempfile.mkdtemp(prefix="shepherd-durability-")

import shepherd_github as g  # noqa: E402
from shepherd_contract import Actor  # noqa: E402

_REAL_STATE_DIR = g.state_dir
g.state_dir = lambda: pathlib.Path(WORK) / "state" / "shepherd"

failures = []
_ASSERTIONS = 0


def check(name, got, want):
    global _ASSERTIONS
    _ASSERTIONS += 1
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
     f"sys.path.insert(0,{str(SKILL)!r});"
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


def _seed(task_id, scope, state, note=""):
    """Force a record into `state`. save() is create-or-advance by contract, so a
    fixture that re-seeds one id across states must use the lock-held primitive --
    which is also exactly what a racing resume() pass does."""
    with g._record_lock(task_id):
        return g._write_record(task_id, g._record_payload(task_id, scope, state, note))

# resume() must be monotonic: re-observing may not reopen a closed objective,
# and ordinary progress may not flatten blocked/needs_human into waiting
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.updated", g.subject_for(repo, num), MINE)

for prior in ("failed", "succeeded", "cancelled"):
    _seed("task-dur-2", scope, prior)
    state, why = g.resume("task-dur-2")
    check(f"terminal {prior} stays {prior}", state, prior)
    check(f"terminal {prior} not re-observed", "not re-observed" in why, True)

for prior in ("blocked", "needs_human", "waiting"):
    _seed("task-dur-2", scope, prior)
    state, _ = g.resume("task-dur-2")
    check(f"progress preserves {prior}", state, prior)

# an asserted actor's merge is surfaced but does not close the objective
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.merged", g.subject_for(repo, num), MINE)
_seed("task-dur-2", scope, "waiting")
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

# The adapter ships no Matrix scheme; the test installs its own verified seam.
from shepherd_contract import register_actor_scheme as _reg_verified  # noqa: E402
_reg_verified("matrix.mxid", verified=True)
VERIFIED = Actor("matrix.mxid", "@qingyun-air.agent:ag2.space")
vscope = g.scope_for("o/r", 2, VERIFIED)
g.save("task-dur-4", vscope, "waiting")
g.observe = lambda repo, num: __import__("shepherd_contract").ObservedEvent(
    "github.pull_request.merged", g.subject_for(repo, num), VERIFIED)
check("verified merge terminates the objective", g.resume("task-dur-4")[0], "succeeded")
g.observe = _REAL_OBSERVE

# --- a record save() accepts must be one load() can resume ---
from shepherd_contract import ResponsibilityScope, Subject  # noqa: E402

_A = Actor(g.ACTOR_SCHEME, "someone@example.com")


def _scope(**kw):
    d = dict(subjects=(g.subject_for("o/r", 3),), actor=_A, watch_conditions=g.WATCH,
             success_conditions=g.SUCCESS, failure_conditions=g.FAILURE)
    d.update(kw)
    return ResponsibilityScope(**d)


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def _rejected_publishes_nothing(tid, sc):
    """Raising is not enough: a validator that runs AFTER the atomic write also
    raises, having already published the invalid record."""
    if not _raises(lambda: g.save(tid, sc, "waiting")):
        return False
    return not (g.state_dir() / f"{tid}.json").exists()


check("empty watch set is refused at SAVE and publishes NO record",
      _rejected_publishes_nothing("task-dur-emptywatch", _scope(
          watch_conditions=frozenset())), True)
check("a scope with no reachable outcome is refused and publishes NO record",
      _rejected_publishes_nothing("task-dur-emptyout", _scope(
          success_conditions=frozenset(), failure_conditions=frozenset())), True)
# the record encodes one subject; a wider scope must not silently narrow on reload
check("a scope this record cannot encode is refused and publishes NO record",
      _rejected_publishes_nothing("task-dur-wide", _scope(subjects=(
          g.subject_for("o/r", 3), Subject("gitlab", "merge_request", "grp/proj!9")))), True)
# round-trip: whatever save() accepted, load() must return unchanged
g.save("task-dur-roundtrip", _scope(), "waiting")
_rt = g.load("task-dur-roundtrip")
check("a successful save round-trips", (_rt["repo"], _rt["number"]), ("o/r", 3))
check("round-trip preserves the watch set",
      tuple(_rt["waiting_for"]), tuple(sorted(g.WATCH)))

# a scalar string must never reach the durable record as N single-char conditions
check("a scalar watch string is refused before save() publishes characters",
      _raises(lambda: g.save("task-dur-scalar", ResponsibilityScope(
          subjects=(g.subject_for("o/r", 3),), actor=_A,
          watch_conditions="github.pull_request.updated",
          success_conditions=g.SUCCESS, failure_conditions=g.FAILURE), "waiting")), True)
check("...and no record was published for it",
      (g.state_dir() / "task-dur-scalar.json").exists(), False)

# --- an unknown observation must not become a concrete outcome ---
# Malformed targets must be rejected BEFORE any credentialed call: count stubs.
_gh_calls = []
_real_gh_for_p2 = g._gh
g._gh = lambda *a: (_gh_calls.append(a), "open false")[1]
_hostile_repos = ["owner/repo/../../victim/private", "repo-only", " owner/repo",
                  "owner/repo ", "owner/repo?x=1", "owner//repo", "owner/..", "",
                  # terminal LF: $ under .match() accepts it; \Z must not
                  "owner/repo\n", "owner\n/repo", "owner/repo\n\n"]
for _hr in _hostile_repos:
    _rejected = False
    try:
        g.observe(_hr, 1)
    except ValueError:
        _rejected = True
    check(f"observe rejects {_hr!r} before any gh call", (_rejected, len(_gh_calls)), (True, 0))
for _vr in ("github/.github", "github/.github-private"):
    _gh_calls.clear()
    try:
        g.observe(_vr, 1)
        _ok = True
    except ValueError:
        _ok = False
    check(f"observe ACCEPTS valid dot-leading repo {_vr!r} (reaches gh)",
          (_ok, len(_gh_calls) > 0), (True, True))
_gh_calls.clear()
for _hn in (-1, 0, True, "007", "1e3"):
    _rejected = False
    try:
        g.resolve_actor("owner/repo", _hn)
    except ValueError:
        _rejected = True
    check(f"resolve_actor rejects PR number {_hn!r} before any gh call",
          (_rejected, len(_gh_calls)), (True, 0))
g._gh = _real_gh_for_p2

_SAVED_GH, _SAVED_ACTOR = g._gh, g.resolve_actor
g.resolve_actor = lambda *a, **k: _A
g._gh = lambda *a, **k: "closed false"
check("CONTROL a valid projection still maps to its outcome",
      g.observe("o/r", 3).event_type, "github.pull_request.closed_unmerged")
g._gh = lambda *a, **k: "closed null"
check("an unknown merged token is refused, not read as unmerged",
      _raises(lambda: g.observe("o/r", 3)), True)
g._gh = lambda *a, **k: "mystery false"
check("an unknown STATE is refused, not read as progress",
      _raises(lambda: g.observe("o/r", 3)), True)
g._gh = lambda *a, **k: ""
check("an empty projection is refused, not read as progress",
      _raises(lambda: g.observe("o/r", 3)), True)
g._gh, g.resolve_actor = _SAVED_GH, _SAVED_ACTOR


import errno  # noqa: E402
import fcntl  # noqa: E402

# A flat `finally` unlocks a lock it may never have held, and a failing unlock
# then skips the close -- leaking the descriptor with the lock still held.

_REAL_FLOCK = fcntl.flock


def _open_fds():
    return {int(n) for n in os.listdir("/dev/fd") if n.isdigit()}


def _fault(task_id, fail_acquire, fail_unlock):
    def fake(fd, op):
        if op == fcntl.LOCK_EX and fail_acquire:
            raise OSError(errno.EAGAIN, "acquire failure")
        if op == fcntl.LOCK_UN and fail_unlock:
            raise OSError(errno.EIO, "unlock failure")
        return _REAL_FLOCK(fd, op)

    before = _open_fds()
    g.fcntl.flock = fake
    try:
        g.save(task_id, scope, "waiting")
    except BaseException:
        pass
    finally:
        g.fcntl.flock = _REAL_FLOCK
    return len(_open_fds() - before), (g.state_dir() / f"{task_id}.json").exists()


check("an acquisition fault closes the descriptor and publishes nothing",
      _fault("task-lock-acq", True, False), (0, False))
check("a release fault still closes the descriptor; the record was already written",
      _fault("task-lock-rel", False, True), (0, True))

# --- save() is create-or-advance, never rebind ---------
def _raises(name, fn):
    global _ASSERTIONS
    _ASSERTIONS += 1
    try:
        fn()
    except ValueError as e:
        return str(e)
    failures.append(f"{name}: did NOT raise ValueError")
    return ""


g.save("task-bind-1", g.scope_for("org/first", 1, MINE), "succeeded", "done")
msg = _raises("terminal record cannot be reopened",
              lambda: g.save("task-bind-1", g.scope_for("org/first", 1, MINE), "waiting"))
check("...and says why", "terminal" in msg, True)
check("the record still reads as it was written", g.load("task-bind-1")["state"], "succeeded")

msg = _raises("a same-id rebind to another PR is refused",
              lambda: g.save("task-bind-1", g.scope_for("org/second", 2, MINE), "waiting"))
check("...naming the binding it holds", "org/first#1" in msg, True)
check("the subject did not move", g.load("task-bind-1")["repo"], "org/first")

g.save("task-bind-2", g.scope_for("org/a", 3, MINE), "waiting", "seed")
_raises("a same-id actor swap is refused",
        lambda: g.save("task-bind-2", g.scope_for("org/a", 3, PEER), "waiting"))
check("the actor did not move", g.load("task-bind-2")["actor_value"], MINE.value)

# advancing STATE on an unchanged binding is the legal update, and must still work
g.save("task-bind-2", g.scope_for("org/a", 3, MINE), "blocked", "advanced")
check("an unchanged binding may advance its state", g.load("task-bind-2")["state"], "blocked")
g.save("task-bind-2", g.scope_for("org/a", 3, MINE), "blocked", "idempotent")
check("re-saving the same state is idempotent", g.load("task-bind-2")["state"], "blocked")

# --- one canonical PR number at every seam -------------
# int() would fold each of these into a DIFFERENT subject than the one supplied.
for bad, why in [("01", "leading zero"), ("\u0661", "Arabic-Indic digit"),
                 (-1, "negative"), (0, "zero"), (True, "bool"), ("1.0", "non-digit")]:
    _raises(f"construction rejects {why} PR number {bad!r}",
            lambda b=bad: g.subject_for("org/repo", b))

check("a canonical number still constructs",
      g.subject_for("org/repo", 7).resource_id, "org/repo#7")
check("a canonical digit STRING is accepted at construction",
      g.subject_for("org/repo", "7").resource_id, "org/repo#7")
_raises("a repo carrying the '#' separator is refused",
        lambda: g.subject_for("org/re#po", 1))

# load(): the persisted schema is stricter still -- a real int, positive
_bad_dir = g.state_dir()
_bad_dir.mkdir(parents=True, exist_ok=True)
_ok = json.loads((_bad_dir / "task-bind-2.json").read_text())
for field, value, why in [("number", -1, "negative"), ("number", 0, "zero"),
                          ("number", True, "bool"), ("repo", "", "blank repo")]:
    tid = f"task-bind-load-{field}-{why.split()[0]}"
    (_bad_dir / f"{tid}.json").write_text(
        json.dumps({**_ok, "task_id": tid, field: value}))
    _raises(f"load rejects a {why}", lambda t=tid: g.load(t))

# public rehydration is its own seam -- load() is not the only way in
_raises("scope_from_saved rejects another provider's record",
        lambda: g.scope_from_saved({**_ok, "provider": "gitlab"}))
_raises("scope_from_saved rejects a blank repo with a bool number",
        lambda: g.scope_from_saved({**_ok, "repo": "", "number": True}))
_raises("scope_from_saved rejects a record with no reachable outcome",
        lambda: g.scope_from_saved({**_ok, "success_conditions": [],
                                    "failure_conditions": []}))
check("scope_from_saved still rehydrates a good record",
      g.scope_from_saved(_ok).subjects[0].resource_id, "org/a#3")

# --- hostile SCALAR SUBCLASSES must not cross any boundary ---
class _EscapingId(str):
    """valid_task_id() sees the safe underlying value; __format__ renders the escape."""
    def __format__(self, spec): return "../../escaped"


class _ForgedStr(str):
    """Overrides equality/hash so a visibly fake scheme can match and read verified."""
    def __eq__(self, other): return True
    def __hash__(self): return hash("git.commit_author_email")


class _EvilInt(int):
    """Overrides comparison so the positivity rule below cannot reject it."""
    def __le__(self, other): return False
    def __lt__(self, other): return False


class _ForgedRepo(str):
    """Hides its '#' from the substring guard; the encoded resource_id then
    rpartitions back to a repo the caller never named."""
    def __contains__(self, item): return False


class _ForgedState(str):
    """Overrides equality so `in SHEPHERD_STATES` passes; json.dump would then
    persist the underlying invalid value for the next process to choke on."""
    def __eq__(self, other): return True
    def __hash__(self): return hash("waiting")


_outside = pathlib.Path(WORK) / "escaped.json"
_raises("hostile task_id (str subclass) is refused before it becomes a path",
        lambda: g.save(_EscapingId("task-safe"), scope, "waiting"))
check("...and NO file was published outside the state dir", _outside.exists(), False)
check("...nor anywhere above it", any(pathlib.Path(WORK).glob("*.json")), False)

_raises("forged Actor scalar (str subclass) is refused at construction",
        lambda: Actor(_ForgedStr("totally.fake"), _ForgedStr("nobody@example.com")))
_raises("an int subclass PR number is refused",
        lambda: g.subject_for("org/repo", _EvilInt(-1)))
_raises("a repo str subclass that hides its '#' is refused",
        lambda: g.subject_for(_ForgedRepo("org/repo#99"), 7))
check("...so no subject can encode a repo the caller never named",
      g.subject_for("org/repo", 7).resource_id.rpartition("#")[0], "org/repo")

# state is DURABLE: membership alone lets a lying subclass reach json.dump, so
# save() would report success for a record the next process cannot load.
_raises("a forged state (str subclass) is refused by public save()",
        lambda: g.save("task-forged-state", scope, _ForgedState("totally.fake")))
check("...and NOTHING was published for it",
      (g.state_dir() / "task-forged-state.json").exists(), False)
g.save("task-state-ok", scope, "waiting")
check("...while a valid state still round-trips through save/load",
      g.load("task-state-ok")["state"], "waiting")
check("a genuine int still constructs", g.subject_for("org/repo", 9).resource_id, "org/repo#9")

# --- observe(): an IMPOSSIBLE (state, merged) pair is malformed evidence -----
_real_gh = g._gh
def _proj(v):
    g._gh = lambda *a: v
    try: return g.observe("org/repo", 1)
    finally: g._gh = _real_gh

g.resolve_actor = lambda repo, num: MINE
for pair, want in (("open false", "github.pull_request.updated"),
                   ("closed false", "github.pull_request.closed_unmerged"),
                   ("closed true", "github.pull_request.merged")):
    check(f"projection {pair!r} maps to {want.split('.')[-1]}", _proj(pair).event_type, want)
_raises("projection 'open true' is REFUSED (merged PRs are never open)",
        lambda: _proj("open true"))

# --- the checked binding must BE the written binding ---
def _type_raises(name, fn):
    global _ASSERTIONS
    _ASSERTIONS += 1
    try:
        fn()
    except TypeError:
        return
    failures.append(f"{name}: did NOT raise TypeError")


class _SwitchingScope(ResponsibilityScope):
    """Reads as org/old#1 while the binding is checked, then as org/new#2 for
    any later payload build — the write must not trust a second read."""
    armed = False
    reads = 0

    def __getattribute__(self, name):
        if name == "subjects" and _SwitchingScope.armed:
            _SwitchingScope.reads += 1
            if _SwitchingScope.reads > 2:
                return (g.subject_for("org/new", 2),)
        return object.__getattribute__(self, name)


g.save("task-switch-1", g.scope_for("org/old", 1, MINE), "waiting", "seed")
_sw = _SwitchingScope(
    subjects=(g.subject_for("org/old", 1),), actor=MINE,
    watch_conditions=g.WATCH, success_conditions=g.SUCCESS,
    failure_conditions=g.FAILURE)
_SwitchingScope.armed = True
_type_raises("a ResponsibilityScope SUBCLASS is refused at save()",
             lambda: g.save("task-switch-1", _sw, "waiting", "advance"))
_SwitchingScope.armed = False
check("...and the durable subject did not move",
      g.load("task-switch-1")["repo"], "org/old")

# Identity, not equality: the dict that passed the under-lock binding check is
# the very object handed to the atomic write.
g.save("task-switch-2", g.scope_for("org/old", 1, MINE), "waiting", "seed")
_built, _written = [], []
_real_payload, _real_atomic = g._record_payload, g._atomic_write


def _spy_payload(*a, **k):
    _built.append(_real_payload(*a, **k))
    return _built[-1]


def _spy_atomic(path, payload):
    _written.append(payload)
    return _real_atomic(path, payload)


g._record_payload, g._atomic_write = _spy_payload, _spy_atomic
try:
    g.save("task-switch-2", g.scope_for("org/old", 1, MINE), "blocked", "advance")
finally:
    g._record_payload, g._atomic_write = _real_payload, _real_atomic
check("save() writes the very payload it checked",
      len(_written) == 1 and _written[0] is _built[0], True)
check("...and that payload is durable", g.load("task-switch-2")["state"], "blocked")

# --- trust binds to the ADAPTER, not to core ----------
# Fresh interpreters: only a new process can witness what core ALONE trusts.
_PROBE = ("from shepherd_contract import Actor;"
          "print(Actor('git.commit_author_email','x').is_discriminating,"
          "Actor('matrix.mxid','x').is_verified)")
_core_alone = subprocess.run(
    [sys.executable, "-c",
     f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r});" + _PROBE],
    capture_output=True, text=True)
check("core ALONE trusts no provider scheme",
      (_core_alone.returncode, _core_alone.stdout.split()), (0, ["False", "False"]))
_with_skill = subprocess.run(
    [sys.executable, "-c",
     f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r});"
     f"sys.path.insert(0, {str(SKILL)!r});"
     "import shepherd_github;" + _PROBE],
    capture_output=True, text=True)
check("importing the skill registers ONLY the Git scheme",
      (_with_skill.returncode, _with_skill.stdout.split()), (0, ["True", "False"]))

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
print(f"PASS: {_ASSERTIONS} assertions, control verified")
