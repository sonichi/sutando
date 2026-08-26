#!/usr/bin/env python3
"""Admission must key on (subject, actor), not subject alone."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import local_task_protocol as ltp  # noqa: E402
from shepherd_contract import (  # noqa: E402
    Actor,
    ObservedEvent,
    ResponsibilityScope,
    SHEPHERD_STATES,
    Subject,
    admit,
    is_terminal,
    proposed_terminal_state,
    terminal_state_for,
)

import shepherd_contract as sc  # noqa: E402

# The contract ships no schemes; this suite declares the ones its fixtures
# use, exactly as an adapter does at its optional edge.
sc.register_actor_scheme("git.commit_author_email")
sc.register_actor_scheme("matrix.mxid", verified=True)

PR = Subject("github", "pull_request", "sonichi/sutando#3291")
OTHER_PR = Subject("github", "pull_request", "sonichi/sutando#3311")

MINE = Actor("git.commit_author_email", "qingyun0327@gmail.com")
PEER = Actor("git.commit_author_email", "qingyun@ag2.ai")
SHARED_LOGIN = Actor("provider.login", "qingyun-wu")

SCOPE = ResponsibilityScope(
    subjects=(PR,),
    actor=MINE,
    watch_conditions=frozenset({"github.check_suite.completed"}),
    success_conditions=frozenset({"github.pull_request.merged"}),
    failure_conditions=frozenset({"github.pull_request.closed_unmerged"}),
)

failures = []
_ASSERTIONS = 0


def check(name, got, want):
    global _ASSERTIONS
    _ASSERTIONS += 1
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


# subject + actor both match -> accepted
check("mine accepted",
      admit(ObservedEvent("github.check_suite.completed", PR, MINE), SCOPE)[0],
      "accepted")

# Under a shared provider account the subject is identical for both actors, so
# subject-only keying admits the wrong work.
check("same subject, peer actor -> ignored",
      admit(ObservedEvent("github.check_suite.completed", PR, PEER), SCOPE)[0],
      "ignored")

# an actor resolved through a shared account cannot discriminate
check("shared-login actor -> ambiguous",
      admit(ObservedEvent("github.check_suite.completed", PR, SHARED_LOGIN), SCOPE)[0],
      "ambiguous")

# unresolved actor is unknown, never "mine"
check("no actor -> ambiguous",
      admit(ObservedEvent("github.check_suite.completed", PR, None), SCOPE)[0],
      "ambiguous")

check("out-of-scope subject -> ignored",
      admit(ObservedEvent("github.check_suite.completed", OTHER_PR, MINE), SCOPE)[0],
      "ignored")

check("unwatched event type -> ignored",
      admit(ObservedEvent("github.issue_comment.created", PR, MINE), SCOPE)[0],
      "ignored")

# a git author email is SELF-DECLARED commit metadata: whoever writes the commit
# sets it, so it may attribute but must not close an objective on its own
check("asserted scheme discriminates", MINE.is_discriminating, True)
check("asserted scheme is NOT verified", MINE.is_verified, False)
check("asserted actor cannot terminate",
      terminal_state_for(ObservedEvent("github.pull_request.merged", PR, MINE), SCOPE),
      None)
check("but the outcome is still surfaced as proposed",
      proposed_terminal_state(ObservedEvent("github.pull_request.merged", PR, MINE), SCOPE),
      "succeeded")

VERIFIED = Actor("matrix.mxid", "@qingyun-air.agent:ag2.space")
VSCOPE = ResponsibilityScope(
    subjects=(PR,), actor=VERIFIED,
    watch_conditions=frozenset({"github.check_suite.completed"}),
    success_conditions=frozenset({"github.pull_request.merged"}),
    failure_conditions=frozenset({"github.pull_request.closed_unmerged"}))
check("verified actor terminates succeeded",
      terminal_state_for(ObservedEvent("github.pull_request.merged", PR, VERIFIED), VSCOPE),
      "succeeded")
check("verified actor terminates failed",
      terminal_state_for(
          ObservedEvent("github.pull_request.closed_unmerged", PR, VERIFIED), VSCOPE),
      "failed")

# someone else's outcome must NOT close this objective, verified or not
check("peer merge does not terminate",
      proposed_terminal_state(ObservedEvent("github.pull_request.merged", PR, PEER), SCOPE),
      None)

# an accepted watched event that is neither success nor failure does not
# terminate: progress is not an outcome
check("accepted non-outcome does not terminate",
      terminal_state_for(ObservedEvent("github.check_suite.completed", PR, MINE), SCOPE),
      None)

# adjacent controls: every way an identity can fail to resolve must be refused
# at construction, so an invalid boundary cannot exist to be matched against
def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


check("blank subject component rejected", raises(lambda: Subject("", "", "")), True)
check("whitespace subject component rejected",
      raises(lambda: Subject("github", "  ", "x/y#1")), True)
check("blank actor scheme rejected", raises(lambda: Actor("", "me@x")), True)
check("whitespace actor value rejected",
      raises(lambda: Actor("git.commit_author_email", "   ")), True)

# strength is an ALLOWLIST: a typoed or unknown scheme must be weak, not strong
check("typoed shared-login scheme is weak",
      Actor("provider_login", "qingyun-wu").is_discriminating, False)
check("unknown scheme is weak",
      Actor("totally.made.up", "whoever").is_discriminating, False)
check("known-strong scheme is strong", MINE.is_discriminating, True)

# contradictory outcome declarations must not be constructible: checking success
# first would otherwise close a failed objective as succeeded
check("overlapping success/failure rejected",
      raises(lambda: ResponsibilityScope(
          subjects=(PR,), actor=MINE,
          success_conditions=frozenset({"done"}),
          failure_conditions=frozenset({"done"}))), True)
check("scope with no subject rejected",
      raises(lambda: ResponsibilityScope(subjects=(), actor=MINE)), True)
check("a non-iterable subjects collection is rejected",
      raises(lambda: ResponsibilityScope(subjects=7, actor=MINE)), True)
check("a bare string subjects collection is rejected",
      raises(lambda: ResponsibilityScope(subjects="github:pull_request:o/r#1", actor=MINE)), True)

check("is_terminal(succeeded)", is_terminal("succeeded"), True)
check("is_terminal(waiting)", is_terminal("waiting"), False)

# the shepherd lifecycle COMPOSES with the task-file lifecycle; it must not
# silently redefine those names
overlap = set(SHEPHERD_STATES) & set(ltp.LIFECYCLE_STATES)
check("no state-name collision with local_task_protocol", overlap, set())

# A frozen dataclass still holds the caller's container; snapshot or the scope moves.
_subs = [PR]
_cond = {"github.pull_request.updated"}
_alias = ResponsibilityScope(subjects=_subs, actor=SCOPE.actor, watch_conditions=_cond,
                             success_conditions={"github.pull_request.merged"},
                             failure_conditions=frozenset())
_other = Subject(PR.provider, PR.kind, PR.resource_id + "-other")
_ev = ObservedEvent("github.pull_request.updated", _other, SCOPE.actor)
_before = admit(_ev, _alias)[0]
_subs.append(_other)
_cond.add("mutated")
check("a caller's alias cannot widen an accepted scope", admit(_ev, _alias)[0], _before)
check("...and the condition set is unchanged too", "mutated" in _alias.watch_conditions, False)


def _rejects(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


check("a scalar string is refused, not iterated as characters",
      _rejects(lambda: ResponsibilityScope(
          subjects=(PR,), actor=SCOPE.actor,
          watch_conditions="github.pull_request.updated",
          success_conditions={"github.pull_request.merged"},
          failure_conditions=frozenset())), True)

# negative control: the harness must be able to report a failure at all
_probe = len(failures)
check("CONTROL (expected to fail)", "x", "y")
if len(failures) != _probe + 1:
    print("FAIL: control did not register — this harness cannot detect a failure")
    sys.exit(1)
failures.pop()

# An ASSERTED identity must not satisfy a scope demanding a VERIFIED one. Both
# schemes are STRONG, so this reaches Actor.matches(), not the discriminating gate.
_ID = "qingyun-air"
_verified_scope = ResponsibilityScope(
    subjects=(PR,), actor=Actor("matrix.mxid", _ID),
    watch_conditions=SCOPE.watch_conditions,
    success_conditions=SCOPE.success_conditions,
    failure_conditions=SCOPE.failure_conditions)
_asserted = Actor("git.commit_author_email", _ID)
check("both schemes are strong, so matches() is actually reached",
      (_asserted.is_discriminating, _verified_scope.actor.is_discriminating), (True, True))
check("a self-declared identity does not satisfy a verified-identity scope",
      admit(ObservedEvent("github.pull_request.merged", PR, _asserted), _verified_scope)[0],
      "ignored")
check("...and the same value under two schemes is not the same actor",
      _asserted.matches(_verified_scope.actor), False)


# A duck-typed stand-in satisfies every attribute the decision functions read, so
# without a TYPE check `is_verified` is whatever the caller says it is.
class _ForeignActor:
    scheme, value = "totally.fake", "attacker"
    is_discriminating = is_verified = True

    def matches(self, other):
        return True


def _rejects(thunk):
    try:
        thunk()
    except ValueError:
        return True
    return False


check("a scope cannot be built without a real Actor",
      _rejects(lambda: ResponsibilityScope(
          subjects=(PR,), actor=None,
          watch_conditions=SCOPE.watch_conditions,
          success_conditions=SCOPE.success_conditions,
          failure_conditions=SCOPE.failure_conditions)), True)
check("a foreign object cannot self-certify as the scope's actor",
      _rejects(lambda: ResponsibilityScope(
          subjects=(PR,), actor=_ForeignActor(),
          watch_conditions=SCOPE.watch_conditions,
          success_conditions=SCOPE.success_conditions,
          failure_conditions=SCOPE.failure_conditions)), True)
check("an event cannot carry a non-Subject subject or a foreign actor",
      (_rejects(lambda: ObservedEvent("github.pull_request.merged", "o/r#1", _asserted)),
       _rejects(lambda: ObservedEvent("github.pull_request.merged", PR, _ForeignActor()))),
      (True, True))


# A subclass inherits the constructor's blessing but can override the trust
# predicates, so identity must be an EXACT type check, never isinstance().
class _EvilActor(Actor):
    @property
    def is_discriminating(self):
        return True

    @property
    def is_verified(self):
        return True

    def matches(self, other):
        return True


class _EvilSubject(Subject):
    def matches(self, other):
        return True


check("an Actor SUBCLASS cannot self-certify into a scope",
      _rejects(lambda: ResponsibilityScope(
          subjects=(PR,), actor=_EvilActor("totally.fake", "attacker"),
          watch_conditions=SCOPE.watch_conditions,
          success_conditions=SCOPE.success_conditions,
          failure_conditions=SCOPE.failure_conditions)), True)
check("a Subject SUBCLASS cannot widen what a scope covers",
      _rejects(lambda: ResponsibilityScope(
          subjects=(_EvilSubject(PR.provider, PR.kind, PR.resource_id),), actor=SCOPE.actor,
          watch_conditions=SCOPE.watch_conditions,
          success_conditions=SCOPE.success_conditions,
          failure_conditions=SCOPE.failure_conditions)), True)
check("an event cannot carry a subclassed subject or actor either",
      (_rejects(lambda: ObservedEvent("github.pull_request.merged",
                                      _EvilSubject(PR.provider, PR.kind, PR.resource_id), _asserted)),
       _rejects(lambda: ObservedEvent("github.pull_request.merged", PR,
                                      _EvilActor("totally.fake", "attacker")))),
      (True, True))


check("source_id must be a string; '' stays legal for an unresolved id",
      (_rejects(lambda: ObservedEvent("github.pull_request.merged", PR, _asserted, 7)),
       _rejects(lambda: ObservedEvent("github.pull_request.merged", PR, _asserted, None)),
       _rejects(lambda: ObservedEvent("github.pull_request.merged", PR, _asserted, "  ")),
       ObservedEvent("github.pull_request.merged", PR, _asserted).source_id),
      (True, True, True, ""))


# --- the TOP-LEVEL event/scope objects are seams too ---
# A scope SUBCLASS inherits the nested blessing yet overrides what admit() trusts.
class _EvilScope(ResponsibilityScope):
    def covers_subject(self, subject):
        return True


class _EvilEvent(ObservedEvent):
    pass


def _type_rejects(thunk):
    try:
        thunk()
    except TypeError:
        return True
    return False


_INSIDE = Subject("github", "pull_request", "org/inside#1")
_OUTSIDE_EVENT = ObservedEvent(
    "github.pull_request.merged",
    Subject("github", "pull_request", "org/outside#2"), MINE)
_evil = _EvilScope(
    subjects=(_INSIDE,), actor=MINE,
    watch_conditions=SCOPE.watch_conditions,
    success_conditions=SCOPE.success_conditions,
    failure_conditions=SCOPE.failure_conditions)
check("a scope SUBCLASS cannot admit an out-of-scope subject via admit()",
      _type_rejects(lambda: admit(_OUTSIDE_EVENT, _evil)), True)
check("...nor propose a terminal state for it",
      _type_rejects(lambda: proposed_terminal_state(_OUTSIDE_EVENT, _evil)), True)
check("...nor terminate it",
      _type_rejects(lambda: terminal_state_for(_OUTSIDE_EVENT, _evil)), True)
check("an ObservedEvent SUBCLASS is refused at admit()",
      _type_rejects(lambda: admit(
          _EvilEvent("github.pull_request.merged", PR, MINE), SCOPE)), True)
check("an exact event/scope pair still decides (control)",
      admit(ObservedEvent("github.pull_request.merged", PR, MINE), SCOPE)[0],
      "accepted")


class _ForgedState(str):
    """Overrides equality so membership in TERMINAL_STATES passes for a value
    that is not a shepherd state at all."""
    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("succeeded")


check("is_terminal refuses a forged str subclass",
      is_terminal(_ForgedState("totally.fake")), False)
check("is_terminal refuses a non-str outright", is_terminal(None), False)
check("is_terminal still recognises a genuine terminal state (control)",
      is_terminal("succeeded"), True)


# --- the scheme sets are a SEAM, not provider knowledge baked into core -------
# Registration mutates module state, so this section stays LAST.
check("an unregistered scheme is weak by default",
      Actor("gitlab.job_token", "x").is_discriminating, False)
sc.register_actor_scheme("gitlab.job_token")
check("a registered asserted scheme discriminates",
      Actor("gitlab.job_token", "x").is_discriminating, True)
check("...but is not verified", Actor("gitlab.job_token", "x").is_verified, False)
sc.register_actor_scheme("oidc.subject", verified=True)
check("a registered verified scheme is verified",
      Actor("oidc.subject", "x").is_verified, True)
check("re-registering at the same strength is a no-op",
      (sc.register_actor_scheme("gitlab.job_token"),
       Actor("gitlab.job_token", "x").is_verified), (None, False))
check("re-registering at the OPPOSITE strength is refused",
      _rejects(lambda: sc.register_actor_scheme("gitlab.job_token", verified=True)), True)
check("a blank scheme cannot be registered",
      _rejects(lambda: sc.register_actor_scheme("  ")), True)

# Trust from a FLAG must require an exact bool: any truthy config-shaped value
# ('false', 1, ...) must raise, never grant verified authority.
for _hostile in ("false", "true", 1, 0, None, [1]):
    def _reg(v=_hostile):
        sc.register_actor_scheme(f"hostile.{type(v).__name__}.{v!r}", verified=v)
    try:
        _reg(); _raised = None
    except TypeError:
        _raised = TypeError
    except Exception as e:  # noqa: BLE001
        _raised = type(e)
    check(f"non-bool verified flag {type(_hostile).__name__} {_hostile!r} raises TypeError",
          _raised, TypeError)

# Concurrent opposite-strength registration of ONE scheme: exactly one side
# wins and the other raises; the scheme must never land in BOTH sets.
import threading as _th
_errors, _barrier = [], _th.Barrier(2)
def _race(flag):
    try:
        _barrier.wait()
        sc.register_actor_scheme("race.scheme", verified=flag)
    except ValueError as e:
        _errors.append(e)
_t1 = _th.Thread(target=_race, args=(True,)); _t2 = _th.Thread(target=_race, args=(False,))
_t1.start(); _t2.start(); _t1.join(); _t2.join()
_in_both = ("race.scheme" in sc.ASSERTED_ACTOR_SCHEMES
            and "race.scheme" in sc.VERIFIED_ACTOR_SCHEMES)
check("concurrent opposite-strength registration never lands in both sets",
      _in_both, False)
check("...and the loser raised rather than silently coexisting",
      (len(_errors), "race.scheme" in sc.STRONG_ACTOR_SCHEMES), (1, True))
check("earlier registrations survive later ones",
      ("git.commit_author_email" in sc.ASSERTED_ACTOR_SCHEMES,
       "matrix.mxid" in sc.VERIFIED_ACTOR_SCHEMES), (True, True))

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"PASS: {_ASSERTIONS} assertions, control verified")
