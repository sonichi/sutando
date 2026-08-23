"""The five delivery identities, as distinct validated value types.

Semantics are frozen in docs/sparrow-delivery-identity.md (B slice 1); this
module gives each identity a type so a task_id cannot silently flow where a
delivery_id is required. Values are opaque strings under a shared grammar:
printable ASCII, no whitespace, no path separators (ids become filenames and
record keys), bounded length.
"""
from __future__ import annotations

from dataclasses import dataclass

_MAX_LEN = 200
_FORBIDDEN = set('/\\\x00')


def _validate(value: str, kind: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} must be a non-empty string")
    if len(value) > _MAX_LEN:
        raise ValueError(f"{kind} exceeds {_MAX_LEN} chars")
    for ch in value:
        if ch in _FORBIDDEN or not ch.isprintable() or ch.isspace():
            raise ValueError(f"{kind} contains forbidden character {ch!r}")


@dataclass(frozen=True)
class _Identity:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, type(self).__name__)

    def __str__(self) -> str:
        return self.value


class DeliveryId(_Identity):
    """One object crossing one boundary, once. Sparrow-owned."""


class TaskId(_Identity):
    """One logical unit of Sutando work. Sutando-owned; Sparrow never mints
    it for its own accounting."""


class AttemptId(_Identity):
    """One physical try at one delivery. Ordered per delivery, never reused."""


class IdempotencyKey(_Identity):
    """One external side-effect, deduplicated. Stable across re-sends."""


class IncarnationId(_Identity):
    """One process lifetime. Attribution only: names who performed an
    attempt, never the work itself (ratchet R1)."""
