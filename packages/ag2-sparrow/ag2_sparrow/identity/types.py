"""The five delivery identities, as distinct validated value types.

Semantics are frozen in docs/sparrow-delivery-identity.md (B slice 1); this
module gives each identity a type so a task_id cannot silently flow where a
delivery_id is required. Values are opaque strings under one shared grammar:
printable ASCII (0x21-0x7E), no whitespace, no path separators (ids become
filenames and record keys), bounded length — plus a per-kind namespace shape.
The constructors in derive.py, the parsers in serialization.py, and these
types all enforce the SAME grammar: a typed value that cannot be re-parsed
cannot be constructed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Optional

_MAX_LEN = 200

# One component: safe chars or fixed-width %XX escapes (uppercase hex),
# at least one unit — empty components are constructor-impossible.
_COMPONENT = r"(?:[^%@#+~:/\\]|%[0-9A-F]{2})+"
_DELIVERY_BASE = rf"(?:d|legacy):{_COMPONENT}@{_COMPONENT}(?:\+r[1-9][0-9]*)*"
# delivery_core's shipped key <item_id>#<epoch>, unescaped and OPAQUE: path
# separators included. Disjoint from "e:...@...", which has no '#'.
_LEGACY_KEY = r".+#[0-9]+"


def _validate(value: str, kind: str, *, charset: bool = True,
              bounded: bool = True) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} must be a non-empty string")
    if bounded and len(value) > _MAX_LEN:
        raise ValueError(f"{kind} exceeds {_MAX_LEN} chars")
    if not charset:
        return
    for ch in value:
        if not 0x21 <= ord(ch) <= 0x7E or ch in "/\\":
            raise ValueError(f"{kind} contains forbidden character {ch!r}")


@dataclass(frozen=True)
class _Identity:
    value: str

    _PATTERN: ClassVar[Optional[re.Pattern]] = None
    # Bytes an earlier release already shipped byte-for-byte: neither the
    # charset nor the length rule may apply — narrowing strands live deliveries.
    _OPAQUE: ClassVar[Optional[re.Pattern]] = None

    def __post_init__(self) -> None:
        kind = type(self).__name__
        opaque = type(self)._OPAQUE
        is_opaque = bool(opaque is not None and isinstance(self.value, str)
                         and opaque.fullmatch(self.value))
        _validate(self.value, kind, charset=not is_opaque, bounded=not is_opaque)
        if is_opaque:
            return
        pattern = type(self)._PATTERN
        if pattern is not None and not pattern.fullmatch(self.value):
            raise ValueError(f"{kind} grammar rejects {self.value!r}")

    def __str__(self) -> str:
        return self.value


class DeliveryId(_Identity):
    """One object crossing one boundary, once. Sparrow-owned."""

    _PATTERN = re.compile(_DELIVERY_BASE)


class TaskId(_Identity):
    """One logical unit of Sutando work. Sutando-owned; Sparrow never mints
    it for its own accounting."""

    _PATTERN = re.compile(r"task-.+")


class AttemptId(_Identity):
    """One physical try at one delivery. Ordered per delivery, never reused."""

    _PATTERN = re.compile(rf"{_DELIVERY_BASE}#a[1-9][0-9]*")


class IdempotencyKey(_Identity):
    """One external side-effect, deduplicated. Stable across re-sends. Two
    admissible shapes: the canonical e:<task>@<boundary>, and the pre-B
    provider key <item_id>#<epoch> that legacy_idempotency_key preserves."""

    _PATTERN = re.compile(rf"e:{_COMPONENT}@{_COMPONENT}")
    _OPAQUE = re.compile(_LEGACY_KEY, re.DOTALL)  # an item_id may hold newlines


class IncarnationId(_Identity):
    """One process lifetime. Attribution only: names who performed an
    attempt, never the work itself (ratchet R1)."""

    _PATTERN = re.compile(rf"{_COMPONENT}:[0-9]+:[0-9]+")
