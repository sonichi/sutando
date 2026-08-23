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

# SAME subject, DIFFERENT actor -> ignored. Under a shared provider account the
# subject alone is identical for both actors, so keying on it admits the wrong
# work and attributes it here.
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

# terminal evidence
check("my merge terminates succeeded",
      terminal_state_for(ObservedEvent("github.pull_request.merged", PR, MINE), SCOPE),
      "succeeded")

# someone else's merge must NOT close this objective
check("peer merge does not terminate",
      terminal_state_for(ObservedEvent("github.pull_request.merged", PR, PEER), SCOPE),
      None)

check("closed_unmerged terminates failed",
      terminal_state_for(ObservedEvent("github.pull_request.closed_unmerged", PR, MINE), SCOPE),
      "failed")

# an accepted watched event that is neither success nor failure does not
# terminate: progress is not an outcome
check("accepted non-outcome does not terminate",
      terminal_state_for(ObservedEvent("github.check_suite.completed", PR, MINE), SCOPE),
      None)

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

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: 13 assertions, control verified")
