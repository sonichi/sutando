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


def check(name, got, want):
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

check("is_terminal(succeeded)", is_terminal("succeeded"), True)
check("is_terminal(waiting)", is_terminal("waiting"), False)

# the shepherd lifecycle COMPOSES with the task-file lifecycle; it must not
# silently redefine those names
overlap = set(SHEPHERD_STATES) & set(ltp.LIFECYCLE_STATES)
check("no state-name collision with local_task_protocol", overlap, set())

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

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: 27 assertions, control verified")
