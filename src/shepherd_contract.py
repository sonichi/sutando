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

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Optional

# Disjoint from local_task_protocol.LIFECYCLE_STATES by design: that tracks the
# task FILE, this tracks the objective, and a file can outlive neither.
SHEPHERD_STATES = (
    "active",
    "waiting",      # nobody need act; a declared condition is outstanding
    "blocked",      # someone or something else must act first
    "needs_human",
    "succeeded",
    "failed",
    "cancelled",
)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

def _require_text(owner: str, name: str, value) -> None:
    """A coerced str() check passes on ints and keeps the original object, so a
    non-string identity can cross the same boundary a blank one is rejected at."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner}.{name} must be a non-blank string, got {value!r}")


# Anything weaker than a subject+actor match is recorded but never evidence.
ADMISSION = ("accepted", "ignored", "ambiguous")


@dataclass(frozen=True)
class Subject:
    """A provider-native resource this task is responsible for. Every component
    must be non-blank: an unresolved subject is not a subject."""

    provider: str
    kind: str
    resource_id: str     # provider-native id, never a display name

    def __post_init__(self) -> None:
        for name in ("provider", "kind", "resource_id"):
            _require_text("Subject", name, getattr(self, name))

    def matches(self, other: "Subject") -> bool:
        return (
            self.provider == other.provider
            and self.kind == other.kind
            and self.resource_id == other.resource_id
        )


# Discriminating: can tell two actors apart. Self-declared, so it can also be
# SET by whoever writes the record -- attribution, never authorization.
ASSERTED_ACTOR_SCHEMES = frozenset({"git.commit_author_email"})

# Authenticated by the provider, so it may close an objective on its own.
VERIFIED_ACTOR_SCHEMES = frozenset({"matrix.mxid"})

# Closed by construction: an unknown or misspelled scheme is in neither set and
# is therefore weak, never strong by default.
STRONG_ACTOR_SCHEMES = ASSERTED_ACTOR_SCHEMES | VERIFIED_ACTOR_SCHEMES


@dataclass(frozen=True)
class Actor:
    """Who is responsible. `scheme` names how the adapter resolved it, so
    strength is declared and validated rather than inferred."""

    scheme: str
    value: str

    def __post_init__(self) -> None:
        for name in ("scheme", "value"):
            _require_text("Actor", name, getattr(self, name))

    @property
    def is_discriminating(self) -> bool:
        return self.scheme in STRONG_ACTOR_SCHEMES

    @property
    def is_verified(self) -> bool:
        return self.scheme in VERIFIED_ACTOR_SCHEMES

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

    def __post_init__(self) -> None:
        _require_text("ObservedEvent", "event_type", self.event_type)
        # EXACT type, not isinstance: a duck-typed stand-in OR a subclass can supply
        # is_verified/matches and would then decide the trust the allowlist exists to deny.
        if type(self.subject) is not Subject:
            raise ValueError(
                f"ObservedEvent.subject must be exactly a Subject, got {type(self.subject).__name__}")
        if self.actor is not None and type(self.actor) is not Actor:
            raise ValueError(
                f"ObservedEvent.actor must be exactly an Actor or None, "
                f"got {type(self.actor).__name__}")


@dataclass(frozen=True)
class ResponsibilityScope:
    """What a shepherd task accepts responsibility for."""

    subjects: tuple[Subject, ...]
    actor: Actor
    watch_conditions: frozenset[str] = field(default_factory=frozenset)
    success_conditions: frozenset[str] = field(default_factory=frozenset)
    failure_conditions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # frozen=True stops attribute rebinding, not mutation of what was passed in.
        # Snapshot before validating, or a caller's alias edits an accepted scope.
        if isinstance(self.subjects, (str, bytes)) or not isinstance(self.subjects, Iterable):
            raise ValueError(
                f"ResponsibilityScope.subjects must be an iterable of Subject, "
                f"got {type(self.subjects).__name__}")
        object.__setattr__(self, "subjects", tuple(self.subjects))
        for name in ("watch_conditions", "success_conditions", "failure_conditions"):
            value = getattr(self, name)
            # A bare str is iterable, so frozenset() would silently rebuild it as a
            # set of single characters -- each one passing the per-element check.
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise ValueError(
                    f"ResponsibilityScope.{name} must be a non-string collection, "
                    f"got {type(value).__name__}")
            object.__setattr__(self, name, frozenset(value))
            for element in getattr(self, name):
                _require_text(f"ResponsibilityScope.{name}", "element", element)
        for subject in self.subjects:
            if type(subject) is not Subject:
                raise ValueError(
                    f"ResponsibilityScope.subjects element must be exactly a Subject, "
                    f"got {type(subject).__name__}")
        if type(self.actor) is not Actor:
            raise ValueError(
                f"ResponsibilityScope.actor must be exactly an Actor, "
                f"got {type(self.actor).__name__}")
        clash = self.success_conditions & self.failure_conditions
        if clash:
            raise ValueError(
                f"event type(s) declared both success and failure: {sorted(clash)}")
        if not self.subjects:
            raise ValueError("ResponsibilityScope needs at least one subject")

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


def proposed_terminal_state(event: ObservedEvent,
                            scope: ResponsibilityScope) -> Optional[str]:
    """The terminal state this event WOULD imply if its actor were authenticated."""
    decision, _ = admit(event, scope)
    if decision != "accepted":
        return None
    if event.event_type in scope.success_conditions:
        return "succeeded"
    if event.event_type in scope.failure_conditions:
        return "failed"
    return None


def terminal_state_for(event: ObservedEvent, scope: ResponsibilityScope) -> Optional[str]:
    """The state this event terminates the objective in, or None.

    An asserted (self-declared) identity can attribute but cannot close: whoever
    writes the record can set that field, so alone it is a claim, not evidence.
    """
    if not event.actor or not event.actor.is_verified:
        return None
    return proposed_terminal_state(event, scope)


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
