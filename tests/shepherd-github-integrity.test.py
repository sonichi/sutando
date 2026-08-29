#!/usr/bin/env python3
"""The durable record must not rebind its own subject, hold an impossible state,
or lose a concurrent terminal write.

Every case drives the PRODUCTION functions. A test that reimplements the write
recipe proves the recipe, not the code that ships.
"""

import json
import pathlib
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The GitHub adapter lives at the optional skill edge, not in core.
sys.path.insert(0, str(ROOT / "skills" / "pr-shepherd-contract" / "scripts"))

WORK = tempfile.mkdtemp(prefix="shepherd-integrity-")

import shepherd_github as g  # noqa: E402
from shepherd_contract import Actor, ResponsibilityScope  # noqa: E402

g.state_dir = lambda: pathlib.Path(WORK) / "state" / "shepherd"

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def raises(name, fn, exc=Exception):
    try:
        fn()
    except exc:
        return
    except Exception as e:  # wrong type still counts as not-the-contract
        failures.append(f"{name}: raised {type(e).__name__}, want {exc.__name__}")
        return
    failures.append(f"{name}: did NOT raise {exc.__name__}")


def scope_for(repo, number, actor=None):
    return ResponsibilityScope(
        subjects=(g.subject_for(repo, number),),
        actor=actor or Actor(g.ACTOR_SCHEME, "someone@example.com"),
        watch_conditions=g.WATCH,
        success_conditions=g.SUCCESS,
        failure_conditions=g.FAILURE)


# --- 1. the persisted subject may not disagree with the scope -----------------
sc = scope_for("org/repo-a", 1)
g.save("task-integrity-1", sc, "waiting", "seed")
rec = g.load("task-integrity-1")
check("subject.repo derived from scope", rec["repo"], "org/repo-a")
check("subject.number derived from scope", rec["number"], 1)
back = g.scope_from_saved(rec)
check("round-trip subject is the same subject", back.subjects, sc.subjects)

# --- 2. an impossible state may not be written or loaded ----------------------
raises("save rejects a state outside SHEPHERD_STATES",
       lambda: g.save("task-integrity-2", scope_for("org/repo-a", 2), "typo-state"),
       ValueError)

p = g._contract_path("task-integrity-3")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"task_id": "task-integrity-3", "provider": "github",
                         "repo": "org/repo-a", "number": 3,
                         "actor_scheme": g.ACTOR_SCHEME, "actor_value": "x@y.z",
                         "state": "typo-state", "note": "",
                         "waiting_for": [], "success_conditions": [],
                         "failure_conditions": []}))
raises("load rejects a record carrying an invalid state",
       lambda: g.load("task-integrity-3"), ValueError)

p4 = g._contract_path("task-integrity-4")
p4.write_text(json.dumps({"task_id": "task-integrity-4", "state": "waiting"}))
raises("load rejects a record missing required keys",
       lambda: g.load("task-integrity-4"), ValueError)

# --- 3. two concurrent saves must both complete -------------------------------
g.save("task-integrity-5", scope_for("org/repo-a", 5), "waiting", "seed")
errs = []
barrier = threading.Barrier(2)


def racer(note):
    def run():
        try:
            barrier.wait(timeout=5)
            g.save("task-integrity-5", scope_for("org/repo-a", 5), "waiting", note)
        except Exception as e:
            errs.append(type(e).__name__)
    return run


ts = [threading.Thread(target=racer(f"n{i}")) for i in range(2)]
[t.start() for t in ts]
[t.join(10) for t in ts]
check("concurrent save() raises nothing", errs, [])
check("record still parses after the race",
      g.load("task-integrity-5")["state"], "waiting")

# --- 4. resume() must not reopen a terminal state written mid-observation -----
g.save("task-integrity-6", scope_for("org/repo-a", 6), "waiting", "seed")
released = threading.Event()

_real_observe = g.observe


def slow_observe(repo, number):
    # A concurrent pass terminates the objective while this one is on the wire.
    g.save("task-integrity-6", scope_for("org/repo-a", 6), "succeeded", "won the race")
    released.set()
    return g.ObservedEvent(
        event_type="github.pull_request.updated",
        subject=g.subject_for(repo, number),
        actor=Actor(g.ACTOR_SCHEME, "someone@example.com"))


g.observe = slow_observe
try:
    state, why = g.resume("task-integrity-6")
finally:
    g.observe = _real_observe

check("resume returns the concurrent terminal state", state, "succeeded")
check("durable record keeps the terminal state", g.load("task-integrity-6")["state"], "succeeded")

# --- 5. hostile / malformed subjects and records ------------------------------
from shepherd_contract import Subject  # noqa: E402

two = ResponsibilityScope(
    subjects=(g.subject_for("org/repo-a", 7), g.subject_for("org/repo-b", 8)),
    actor=Actor(g.ACTOR_SCHEME, "a@b.c"), watch_conditions=g.WATCH,
    success_conditions=g.SUCCESS, failure_conditions=g.FAILURE)
raises("save rejects a scope with two pull_request subjects",
       lambda: g.save("task-integrity-7", two, "waiting"), ValueError)

malformed = ResponsibilityScope(
    subjects=(Subject(g.PROVIDER, "pull_request", "no-hash-here"),),
    actor=Actor(g.ACTOR_SCHEME, "a@b.c"), watch_conditions=g.WATCH,
    success_conditions=g.SUCCESS, failure_conditions=g.FAILURE)
raises("save rejects a subject id with no #number",
       lambda: g.save("task-integrity-8", malformed, "waiting"), ValueError)


def _write_raw(task_id, obj):
    q = g._contract_path(task_id)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(json.dumps(obj))


base = {"task_id": "x", "provider": "github", "repo": "o/r", "number": 1,
        "actor_scheme": g.ACTOR_SCHEME, "actor_value": "a@b.c",
        "state": "waiting", "note": "",
        # Must itself be VALID: an empty collection here would trip the arity
        # check first and every targeted test below would pass on the wrong error.
        "waiting_for": ["github.pull_request.updated"],
        "success_conditions": ["github.pull_request.merged"],
        "failure_conditions": ["github.pull_request.closed_unmerged"]}

_write_raw("task-integrity-9", [1, 2, 3])
raises("load rejects a non-object record", lambda: g.load("task-integrity-9"), ValueError)

_write_raw("task-integrity-10", {**base, "task_id": "task-integrity-10", "provider": "gitlab"})
raises("load rejects another provider's record",
       lambda: g.load("task-integrity-10"), ValueError)

_write_raw("task-integrity-11", {**base, "task_id": "task-integrity-11", "number": "1"})
raises("load rejects a non-integer number",
       lambda: g.load("task-integrity-11"), ValueError)

check("load of an absent contract is None", g.load("task-integrity-99"), None)

# --- 6. the three resume outcomes still work under the lock -------------------
def _resume_with(task_id, event, seed="waiting"):
    g.save(task_id, scope_for("org/repo-a", 12), seed, "seed")
    real = g.observe
    g.observe = lambda repo, number: event
    try:
        return g.resume(task_id)
    finally:
        g.observe = real


ignored = g.ObservedEvent(
    event_type="github.pull_request.merged",
    subject=g.subject_for("other/repo", 99),
    actor=Actor(g.ACTOR_SCHEME, "a@b.c"))
st, why = _resume_with("task-integrity-12", ignored)
check("an event about another subject does not move the state", st, "waiting")
check("...and says it was ignored", "ignored" in why, True)

# The adapter ships no Matrix scheme; the test installs its own verified seam.
from shepherd_contract import register_actor_scheme as _reg_verified  # noqa: E402
_reg_verified("matrix.mxid", verified=True)
merged_verified = g.ObservedEvent(
    event_type="github.pull_request.merged",
    subject=g.subject_for("org/repo-a", 12),
    actor=Actor("matrix.mxid", "@someone:example.org"))
vscope = ResponsibilityScope(
    subjects=(g.subject_for("org/repo-a", 12),),
    actor=Actor("matrix.mxid", "@someone:example.org"),
    watch_conditions=g.WATCH, success_conditions=g.SUCCESS,
    failure_conditions=g.FAILURE)
g.save("task-integrity-13", vscope, "waiting", "seed")
_real = g.observe
g.observe = lambda repo, number: merged_verified
try:
    st, why = g.resume("task-integrity-13")
finally:
    g.observe = _real
check("a verified merged event terminates", st, "succeeded")
check("and the terminal state is durable", g.load("task-integrity-13")["state"], "succeeded")

merged_asserted = g.ObservedEvent(
    event_type="github.pull_request.merged",
    subject=g.subject_for("org/repo-a", 12),
    actor=Actor(g.ACTOR_SCHEME, "someone@example.com"))
st, why = _resume_with("task-integrity-14", merged_asserted, seed="blocked")
check("an asserted actor cannot terminate", st, "blocked")
check("...and the note names the proposal", "proposed=succeeded" in why, True)

# --- 7. a failed write leaves no temp behind, and the error is not swallowed --
g.save("task-integrity-15", scope_for("org/repo-a", 15), "waiting", "seed")
_real_dump = json.dump


def _boom(*a, **k):
    raise RuntimeError("disk went away mid-write")


json.dump = _boom
try:
    raises("a failed write re-raises", lambda: g.save(
        "task-integrity-15", scope_for("org/repo-a", 15), "waiting"), RuntimeError)
finally:
    json.dump = _real_dump
check("a failed write leaves no .tmp behind",
      [q.name for q in g.state_dir().iterdir() if q.name.endswith(".tmp")], [])
check("the previous record survives a failed write",
      g.load("task-integrity-15")["note"], "seed")

# --- 8. resume guards: absent contract, and one deleted mid-observation -------
check("resume on an absent contract", g.resume("task-integrity-98")[0], "unknown")

g.save("task-integrity-16", scope_for("org/repo-a", 16), "waiting", "seed")
_real2 = g.observe


def _delete_then_observe(repo, number):
    g._contract_path("task-integrity-16").unlink()
    return g.ObservedEvent(event_type="github.pull_request.updated",
                           subject=g.subject_for(repo, number),
                           actor=Actor(g.ACTOR_SCHEME, "someone@example.com"))


g.observe = _delete_then_observe
try:
    st16, why16 = g.resume("task-integrity-16")
finally:
    g.observe = _real2
check("a contract deleted mid-observation is not resurrected", st16, "unknown")
check("...and says so", "disappeared" in why16, True)

# --- 9. an already-terminal record is not re-observed at all ------------------
g.save("task-integrity-17", scope_for("org/repo-a", 17), "succeeded", "done")
_real3 = g.observe


def _must_not_run(repo, number):
    failures.append("terminal record was re-observed")
    raise AssertionError


g.observe = _must_not_run
try:
    st17, why17 = g.resume("task-integrity-17")
finally:
    g.observe = _real3
check("a terminal record short-circuits", st17, "succeeded")
check("...without a network call", "not re-observed" in why17, True)

# --- 10. a concurrent NON-TERMINAL rebind must not be reverted ----------------
# The terminal guard catches one kind of concurrent mutation; a rebind is the other.


def _race(task_id, scope, state, note=""):
    """save() refuses a rebind outright, so only a resume() pass reaches here."""
    with g._record_lock(task_id):
        return g._write_record(task_id, g._record_payload(task_id, scope, state, note))


g.save("task-integrity-18", scope_for("org/old", 1), "waiting", "seed")
_real4 = g.observe


def _rebind_then_observe(repo, number):
    _race("task-integrity-18", scope_for("org/new", 2), "blocked", "rebound")
    return g.ObservedEvent(event_type="github.pull_request.updated",
                           subject=g.subject_for(repo, number),
                           actor=Actor(g.ACTOR_SCHEME, "someone@example.com"))


g.observe = _rebind_then_observe
try:
    st18, why18 = g.resume("task-integrity-18")
finally:
    g.observe = _real4
rec18 = g.load("task-integrity-18")
check("a concurrent rebind survives resume (repo)", rec18["repo"], "org/new")
check("a concurrent rebind survives resume (number)", rec18["number"], 2)
check("resume reports the concurrent state", st18, "blocked")
check("...and says the observation was discarded", "discarded" in why18, True)

# a concurrent actor change is the same class of clobber
g.save("task-integrity-19", scope_for("org/a", 3, Actor(g.ACTOR_SCHEME, "first@x.z")),
       "waiting", "seed")
_real5 = g.observe


def _reactor_then_observe(repo, number):
    _race("task-integrity-19", scope_for("org/a", 3, Actor(g.ACTOR_SCHEME, "second@x.z")),
          "waiting", "new actor")
    return g.ObservedEvent(event_type="github.pull_request.updated",
                           subject=g.subject_for(repo, number),
                           actor=Actor(g.ACTOR_SCHEME, "first@x.z"))


g.observe = _reactor_then_observe
try:
    g.resume("task-integrity-19")
finally:
    g.observe = _real5
check("a concurrent actor rebind is not reverted",
      g.load("task-integrity-19")["actor_value"], "second@x.z")

# --- 11. the record's embedded id must equal the id it is loaded under -------
_write_raw("task-integrity-20", {**base, "task_id": "task-somewhere-else"})
raises("load rejects a record whose embedded task_id differs",
       lambda: g.load("task-integrity-20"), ValueError)

# --- 12. condition collections: scalar str, wrong type, mixed elements -------
# A bare str is iterable, so frozenset() would reconstruct it as characters.
for key in ("waiting_for", "success_conditions", "failure_conditions"):
    tid = f"task-integrity-2{key[0]}x"
    _write_raw(tid, {**base, "task_id": tid, key: "github.pull_request.updated"})
    raises(f"load rejects a scalar string for {key}", lambda t=tid: g.load(t), ValueError)

_write_raw("task-integrity-21", {**base, "task_id": "task-integrity-21",
                                 "waiting_for": ["ok.event", 42]})
raises("load rejects a non-string element in a condition list",
       lambda: g.load("task-integrity-21"), ValueError)
_write_raw("task-integrity-22", {**base, "task_id": "task-integrity-22",
                                 "waiting_for": ["ok.event", "   "]})
raises("load rejects a blank string element",
       lambda: g.load("task-integrity-22"), ValueError)
_write_raw("task-integrity-23", {**base, "task_id": "task-integrity-23",
                                 "waiting_for": {"a": 1}})
raises("load rejects a dict where a list belongs",
       lambda: g.load("task-integrity-23"), ValueError)

# --- 13. identity strings must be present and non-blank ----------------------
for key in ("repo", "actor_scheme", "actor_value"):
    tid = f"task-integrity-3{key[0]}x"
    _write_raw(tid, {**base, "task_id": tid, key: "   "})
    raises(f"load rejects a blank {key}", lambda t=tid: g.load(t), ValueError)

_write_raw("task-integrity-24", {**base, "task_id": "task-integrity-24", "number": True})
raises("load rejects a bool where an int number belongs",
       lambda: g.load("task-integrity-24"), ValueError)

# --- 14. contradictory conditions cannot survive reconstruction --------------
_write_raw("task-integrity-25", {**base, "task_id": "task-integrity-25",
                                 "success_conditions": ["both.event"],
                                 "failure_conditions": ["both.event"]})
raises("load rejects overlapping success/failure conditions",
       lambda: g.load("task-integrity-25"), ValueError)

# scope_from_saved is public: guard it at its own seam, not only via load()
raises("scope_from_saved rejects a scalar condition string",
       lambda: g.scope_from_saved({**base, "waiting_for": "abc.def"}), ValueError)

# --- 15. arity: an empty collection satisfies every element check by vacuity --
# Such a record loads cleanly and can then never progress or complete.
_write_raw("task-integrity-26", {**base, "task_id": "task-integrity-26",
                                 "success_conditions": [], "failure_conditions": []})
raises("load rejects a record with no reachable outcome",
       lambda: g.load("task-integrity-26"), ValueError)

_write_raw("task-integrity-27", {**base, "task_id": "task-integrity-27", "waiting_for": []})
raises("load rejects a record that observes nothing",
       lambda: g.load("task-integrity-27"), ValueError)

raises("scope_from_saved rejects an empty watch set",
       lambda: g.scope_from_saved({**base, "waiting_for": []}), ValueError)

# A success-only contract stays legal: requiring ALL THREE non-empty would
# reject an objective that can succeed but has no defined failure event.
_write_raw("task-integrity-28", {**base, "task_id": "task-integrity-28",
                                 "failure_conditions": []})
check("a success-only contract still loads",
      g.load("task-integrity-28")["task_id"], "task-integrity-28")

# --- 16. the public seam validates elements, not only container and arity -----
raises("scope_from_saved rejects a mixed-type element",
       lambda: g.scope_from_saved({**base, "success_conditions": ["ok", 7]}), ValueError)
raises("scope_from_saved rejects a blank element",
       lambda: g.scope_from_saved({**base, "failure_conditions": ["  "]}), ValueError)
check("scope_from_saved still accepts a well-formed record",
      sorted(g.scope_from_saved(base).success_conditions), sorted(base["success_conditions"]))

# --- 17. terminal-LF never reaches persistence: repo via save(), task id via path gate
_lf_scope_err = False
try:
    g.save("task-integrity-lf-repo", scope_for("org/repo-a\n", 17), "waiting", "seed")
except ValueError:
    _lf_scope_err = True
check("save() rejects a terminal-LF repo before publication",
      (_lf_scope_err, [q.name for q in (g.state_dir()).glob("*") if "task-integrity-lf-repo" in q.name]),
      (True, []))
_lf_tid_err = False
try:
    g.save("task-integrity-lf-tid\n", scope_for("org/repo-a", 18), "waiting", "seed")
except ValueError:
    _lf_tid_err = True
_matches_lf = [q.name for q in (g.state_dir()).glob("*") if "task-integrity-lf-tid" in q.name]
check("save() rejects a terminal-LF task id before publication",
      (_lf_tid_err, _matches_lf), (True, []))

print(f"integrity: {len(failures)} failure(s)")
for f in failures:
    print("  FAIL", f)
sys.exit(1 if failures else 0)
