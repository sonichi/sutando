"""Parse/format round-trip and record embedding for the five identities.

Formatting is str(); parsing delegates to the type constructors, which own
the one canonical grammar (types.py) — so a foreign string cannot masquerade
as a derived identity, and every value a constructor accepts re-parses.
Record embedding adds explicit fields to a JSON-style dict without touching
existing keys — how slice 2 introduces the field into outbox-shaped records
without breaking the vendored-twin rule.
"""
from __future__ import annotations

from .types import (AttemptId, DeliveryId, IdempotencyKey, IncarnationId,
                    TaskId)


def _parse(cls, value):
    if not isinstance(value, str):
        raise ValueError(f"not a valid {cls.__name__}: {value!r}")
    return cls(value)


def parse_delivery_id(value: str) -> DeliveryId:
    return _parse(DeliveryId, value)


def parse_attempt_id(value: str) -> AttemptId:
    return _parse(AttemptId, value)


def parse_idempotency_key(value: str) -> IdempotencyKey:
    return _parse(IdempotencyKey, value)


def parse_task_id(value: str) -> TaskId:
    return _parse(TaskId, value)


def parse_incarnation_id(value: str) -> IncarnationId:
    return _parse(IncarnationId, value)


_FIELDS = {
    "delivery_id": (DeliveryId, parse_delivery_id),
    "task_id": (TaskId, parse_task_id),
    "attempt_id": (AttemptId, parse_attempt_id),
    "idempotency_key": (IdempotencyKey, parse_idempotency_key),
    "incarnation_id": (IncarnationId, parse_incarnation_id),
}


def to_record_fields(**identities) -> dict:
    """Explicit identity fields for embedding in a record dict. Keyword names
    must be field names from the frozen doc; values must be the right type."""
    out = {}
    for name, value in identities.items():
        # Validate the NAME before the None short-circuit: a misspelled
        # optional field would otherwise be silently discarded.
        if name not in _FIELDS:
            raise ValueError(f"unknown identity field {name!r}")
        if value is None:
            continue
        want = _FIELDS[name][0]
        if type(value) is not want:
            raise TypeError(f"{name} must be {want.__name__}, "
                            f"got {type(value).__name__}")
        out[name] = value.value
    return out


def identity_fields_from_record(record: dict) -> dict:
    """Parse whichever identity fields a record carries; absent fields are
    absent in the result, malformed ones raise."""
    out = {}
    for name, (_cls, parse) in _FIELDS.items():
        if name in record:
            out[name] = parse(record[name])
    return out
