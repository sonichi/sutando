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
import threading
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
    """EXACT str, not isinstance: a str SUBCLASS can override __eq__/__hash__/
    __format__, so a value that reads as verified here can compare, hash or
    render as something else at the seam that trusts it."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{owner}.{name} must be a non-blank str "
                         f"(exact type), got {type(value).__name__}: {value!r}")


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


# Discriminating: can tell two actors apart -- attribution, never authorization.
# Ships EMPTY: schemes are provider policy, registered by adapters at their edge.
ASSERTED_ACTOR_SCHEMES = frozenset()

# Authenticated by the provider, so it may close an objective on its own.
# Ships EMPTY for the same reason.
VERIFIED_ACTOR_SCHEMES = frozenset()

# Closed by construction: an unknown, misspelled or unregistered scheme is in
# neither set and is therefore weak, never strong by default.
STRONG_ACTOR_SCHEMES = ASSERTED_ACTOR_SCHEMES | VERIFIED_ACTOR_SCHEMES


_REGISTRY_LOCK = threading.Lock()


def register_actor_scheme(scheme: str, *, verified: bool = False) -> None:
    """Adapter seam: declare an actor-resolution scheme as discriminating.
    Verified schemes may close an objective on their own; asserted schemes
    attribute but cannot close. Re-registering at the same strength is a no-op;
    changing a scheme's strength is refused -- trust never moves by accident."""
    global ASSERTED_ACTOR_SCHEMES, VERIFIED_ACTOR_SCHEMES, STRONG_ACTOR_SCHEMES
    _require_text("register_actor_scheme", "scheme", scheme)
    if type(verified) is not bool:
        raise TypeError(f"register_actor_scheme verified must be a bool, got "
                        f"{type(verified).__name__}: a truthy flag must never grant trust")
    with _REGISTRY_LOCK:
        other = ASSERTED_ACTOR_SCHEMES if verified else VERIFIED_ACTOR_SCHEMES
        if scheme in other:
            raise ValueError(f"actor scheme {scheme!r} is already registered at the "
                             f"opposite strength; refusing to change its trust")
        if verified:
            VERIFIED_ACTOR_SCHEMES = VERIFIED_ACTOR_SCHEMES | {scheme}
        else:
            ASSERTED_ACTOR_SCHEMES = ASSERTED_ACTOR_SCHEMES | {scheme}
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
        # "" means the adapter resolved no id and stays legal; anything non-str, or a
        # blank that only LOOKS like an id, would key idempotency on a value that is not one.
        if type(self.source_id) is not str or (self.source_id and not self.source_id.strip()):
            raise ValueError(
                f"ObservedEvent.source_id must be a string ('' when unresolved), "
                f"got {self.source_id!r}")


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


def _require_admissible(event: ObservedEvent, scope: ResponsibilityScope) -> None:
    """TypeError, not a decision: a subclass can override every predicate the
    decision reads, so no verdict derived from it means anything — even
    `ambiguous` would let a forged scope keep being polled as legitimate."""
    if type(event) is not ObservedEvent:
        raise TypeError(f"event must be exactly ObservedEvent, "
                        f"got {type(event).__name__}")
    if type(scope) is not ResponsibilityScope:
        raise TypeError(f"scope must be exactly ResponsibilityScope, "
                        f"got {type(scope).__name__}")


def admit(event: ObservedEvent, scope: ResponsibilityScope) -> tuple[str, str]:
    """Decide whether `event` belongs to `scope`. Returns (decision, reason).

    Fails toward `ambiguous`, never toward `accepted`: an event that cannot be
    attributed is not evidence, and silently claiming it is how one actor's
    work becomes another's record.
    """
    _require_admissible(event, scope)
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
    _require_admissible(event, scope)
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
    _require_admissible(event, scope)
    if not event.actor or not event.actor.is_verified:
        return None
    return proposed_terminal_state(event, scope)


def require_shepherd_state(value: object, where: str) -> str:
    """EXACT str BEFORE membership. A str subclass overriding __eq__ satisfies
    `in SHEPHERD_STATES` while json.dump persists its underlying value."""
    if type(value) is not str:
        raise ValueError(f"{where}: state must be an exact str, "
                         f"got {type(value).__name__}: {value!r}")
    if value not in SHEPHERD_STATES:
        raise ValueError(f"{where}: state {value!r} is not in SHEPHERD_STATES")
    return value


def is_terminal(state: object) -> bool:
    # Same exact-str-before-membership policy as require_shepherd_state: a str
    # subclass overriding __eq__/__hash__ must not read as terminal.
    return type(state) is str and state in TERMINAL_STATES
