"""Shepherd contract: the responsibility scope a task accepts for an external
objective, and the admission rule deciding which observed events belong to it.

A normal task ends when it produces a result. A shepherd task ends when its
external subject reaches a declared terminal state, so it must say up front
WHAT it is responsible for, WHOSE work it is, and WHICH observations count.
That declaration is also the boundary for anything derived from the task
later: an event outside it was never this task's to answer for.

Two identities are required, not one. A subject alone ("this repository, this
pull request") does not determine responsibility wherever several actors push
under one provider account -- the observations would be indistinguishable and
one actor's work would be attributed to another. Admission therefore keys on
(subject, actor), and each adapter declares how it resolves actor, because the
provider's own account field is not always the answer.

Stdlib only, no I/O: this is the schema and the decision rule. Persistence,
polling and provider calls belong to the adapters that bind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The shepherd objective's lifecycle. DISTINCT from local_task_protocol's
# LIFECYCLE_STATES, which tracks the task FILE (pending/result_written/
# archived); a task file can be archived while its objective is still open.
SHEPHERD_STATES = (
    "active",      # doing work now
    "waiting",     # no one need act; a declared condition is outstanding
    "blocked",     # someone or something else must act first
    "needs_human", # owner authorization or judgement required
    "succeeded",
    "failed",
    "cancelled",
)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

# An event is admitted only when subject AND actor both resolve. Anything
# weaker is recorded but never treated as this task's evidence.
ADMISSION = ("accepted", "ignored", "ambiguous")


@dataclass(frozen=True)
class Subject:
    """A provider-native resource this task is responsible for."""

    provider: str        # "github", "ag2space", ...
    kind: str            # "pull_request", "room", ...
    resource_id: str     # "sonichi/sutando#3303" -- provider-native, never a name

    def matches(self, other: "Subject") -> bool:
        return (
            self.provider == other.provider
            and self.kind == other.kind
            and self.resource_id == other.resource_id
        )


@dataclass(frozen=True)
class Actor:
    """Who is responsible. `scheme` names how the adapter resolved it, so a
    reader can tell a strong identity from a weak one without guessing."""

    scheme: str     # "git.commit_author_email", "matrix.mxid", "provider.login"
    value: str

    # A provider account shared by several actors cannot discriminate, so an
    # adapter must not resolve actor through it.
    WEAK_SCHEMES = frozenset({"provider.login"})

    @property
    def is_discriminating(self) -> bool:
        return self.scheme not in Actor.WEAK_SCHEMES and bool(self.value)

    def matches(self, other: "Actor") -> bool:
        return self.scheme == other.scheme and self.value == other.value


@dataclass(frozen=True)
class ObservedEvent:
    """Something the world reported. `actor` is None when the adapter could not
    resolve one -- that is unknown, never 'mine'."""

    event_type: str
    subject: Subject
    actor: Optional[Actor] = None
    source_id: str = ""          # provider-native id; the idempotency key


@dataclass(frozen=True)
class ResponsibilityScope:
    """What a shepherd task accepts responsibility for."""

    subjects: tuple[Subject, ...]
    actor: Actor
    watch_conditions: frozenset[str] = field(default_factory=frozenset)
    success_conditions: frozenset[str] = field(default_factory=frozenset)
    failure_conditions: frozenset[str] = field(default_factory=frozenset)

    def covers_subject(self, subject: Subject) -> bool:
        return any(s.matches(subject) for s in self.subjects)


def admit(event: ObservedEvent, scope: ResponsibilityScope) -> tuple[str, str]:
    """Decide whether `event` belongs to `scope`. Returns (decision, reason).

    Fails toward `ambiguous`, never toward `accepted`: an event that cannot be
    attributed is not evidence, and silently claiming it is how one actor's
    work becomes another's record.
    """
    if not scope.covers_subject(event.subject):
        return "ignored", "subject outside scope"

    if event.event_type not in scope.watch_conditions and not _is_outcome(event, scope):
        return "ignored", "event type not watched"

    if event.actor is None:
        return "ambiguous", "actor unresolved for an in-scope subject"

    if not event.actor.is_discriminating:
        return "ambiguous", f"actor scheme {event.actor.scheme!r} cannot discriminate"

    if not event.actor.matches(scope.actor):
        return "ignored", "in-scope subject, different actor"

    return "accepted", "subject and actor both match"


def _is_outcome(event: ObservedEvent, scope: ResponsibilityScope) -> bool:
    return (
        event.event_type in scope.success_conditions
        or event.event_type in scope.failure_conditions
    )


def terminal_state_for(event: ObservedEvent, scope: ResponsibilityScope) -> Optional[str]:
    """The shepherd state this event terminates the objective in, or None.

    Only an ACCEPTED event may terminate: an unattributable outcome must not
    close someone else's responsibility.
    """
    decision, _ = admit(event, scope)
    if decision != "accepted":
        return None
    if event.event_type in scope.success_conditions:
        return "succeeded"
    if event.event_type in scope.failure_conditions:
        return "failed"
    return None


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
