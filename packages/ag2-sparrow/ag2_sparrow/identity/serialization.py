"""Parse/format round-trip and record embedding for the five identities.

Formatting is str(); parsing validates the namespace grammar from derive.py
so a foreign string cannot masquerade as a derived identity. Record embedding
adds explicit fields to a JSON-style dict without touching existing keys —
how slice 2 introduces the field into outbox-shaped records without breaking
the vendored-twin rule.
"""
from __future__ import annotations

import re

from .types import (AttemptId, DeliveryId, IdempotencyKey, IncarnationId,
                    TaskId)

_COMPONENT = r"[^%@#+~:\s/\\]*(?:%[0-9A-F]{2}[^%@#+~:\s/\\]*)*"
_DELIVERY_BASE = rf"(?:d|legacy):{_COMPONENT}@{_COMPONENT}"
_DELIVERY = re.compile(rf"^{_DELIVERY_BASE}(?:\+r[1-9][0-9]*)*$")
_ATTEMPT = re.compile(rf"^{_DELIVERY_BASE}(?:\+r[1-9][0-9]*)*#a[1-9][0-9]*$")
_IDEMPOTENCY = re.compile(rf"^e:{_COMPONENT}@{_COMPONENT}$")
_TASK = re.compile(r"^task-\S+$")
_INCARNATION = re.compile(rf"^{_COMPONENT}:[0-9]+:[0-9]+$")


def _parse(pattern: re.Pattern, cls, value: str):
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f"not a valid {cls.__name__}: {value!r}")
    return cls(value)


def parse_delivery_id(value: str) -> DeliveryId:
    return _parse(_DELIVERY, DeliveryId, value)


def parse_attempt_id(value: str) -> AttemptId:
    return _parse(_ATTEMPT, AttemptId, value)


def parse_idempotency_key(value: str) -> IdempotencyKey:
    return _parse(_IDEMPOTENCY, IdempotencyKey, value)


def parse_task_id(value: str) -> TaskId:
    return _parse(_TASK, TaskId, value)


def parse_incarnation_id(value: str) -> IncarnationId:
    return _parse(_INCARNATION, IncarnationId, value)


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
        if value is None:
            continue
        if name not in _FIELDS:
            raise ValueError(f"unknown identity field {name!r}")
        want = _FIELDS[name][0]
        if not isinstance(value, want):
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
