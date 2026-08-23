"""Canonical derivation of the five identities — pure functions only.

Every function here is a pure function of its arguments (ratchet R3), and no
argument may carry wall-clock or process material into a logical identity
(ratchet R1): this module imports neither time, os, random, uuid nor datetime,
and the test suite pins that. Incarnation material enters exactly one
constructor — incarnation_id_from — whose OUTPUT is attribution-only.

Namespace prefixes keep the kinds parseable and collision-free:
    d:<task>@<boundary>            fresh delivery
    legacy:<content>@<boundary>    R3 mapping of a pre-B artifact
    <delivery>+r<n>                post-reconciliation re-send successor
    <delivery>#a<n>                attempt n of a delivery
    e:<task>@<boundary>            idempotency key (stable across re-sends)
"""
from __future__ import annotations

from .types import (AttemptId, DeliveryId, IdempotencyKey, IncarnationId,
                    TaskId)

# Reserved separators; escaped in raw components so the grammar is injective.
_RESERVED = "%@#+~:"


def escape_component(raw: str) -> str:
    if not raw:
        raise ValueError("identity component must be non-empty")
    out = []
    for ch in raw:
        if ch in _RESERVED:
            out.append(f"%{ord(ch):02X}")
        elif ch in "/\\" or ch.isspace() or not ch.isprintable():
            out.append(f"%{ord(ch):02X}")
        else:
            out.append(ch)
    return "".join(out)


def ingress_task_id(instance: str, provider_event_id: str) -> TaskId:
    """Injective normalized ingress identity: the same provider event maps to
    the same task file on every replay (the shipped ag2space shape
    task-<inst>~<broker_id>, generalized). This is the constructor that ends
    wall-clock task minting at the bridges."""
    return TaskId(f"task-{escape_component(instance)}~"
                  f"{escape_component(provider_event_id)}")


def delivery_id(task: TaskId, boundary: str) -> DeliveryId:
    """One object crossing one boundary. Deterministic from (task, boundary);
    a retry or crash recovery re-derives the same id (ratchet R2)."""
    return DeliveryId(f"d:{escape_component(task.value)}@"
                      f"{escape_component(boundary)}")


def legacy_delivery_id(content_key: str, boundary: str) -> DeliveryId:
    """R3 mapping for a pre-B artifact with no delivery_id: a pure function
    of its stable content, so the same artifact maps to the same id on every
    read, on every host. content_key is whatever stable identity the artifact
    already carries (task_id, filename#mtime_ns, ...)."""
    return DeliveryId(f"legacy:{escape_component(content_key)}@"
                      f"{escape_component(boundary)}")


def resend_delivery_id(predecessor: DeliveryId, ordinal: int) -> DeliveryId:
    """Post-reconciliation re-send: a NEW delivery with lineage to its
    predecessor (R2's only minting row). Ordinal is 1-based per predecessor."""
    if ordinal < 1:
        raise ValueError("resend ordinal is 1-based")
    return DeliveryId(f"{predecessor.value}+r{ordinal}")


def attempt_id(delivery: DeliveryId, ordinal: int) -> AttemptId:
    """One physical try. Ordered per delivery, 1-based, never reused."""
    if ordinal < 1:
        raise ValueError("attempt ordinal is 1-based")
    return AttemptId(f"{delivery.value}#a{ordinal}")


def idempotency_key(task: TaskId, boundary: str) -> IdempotencyKey:
    """One external side-effect. A re-send successor keeps the SAME key —
    derive from the task and boundary, never from the delivery lineage."""
    return IdempotencyKey(f"e:{escape_component(task.value)}@"
                          f"{escape_component(boundary)}")


def incarnation_id_from(worker: str, pid: int, start_usec: int) -> IncarnationId:
    """Attribution identity for one process lifetime. The caller supplies the
    process material; nothing derived here may feed the constructors above."""
    if pid < 0 or start_usec < 0:
        raise ValueError("pid and start_usec must be non-negative")
    return IncarnationId(f"{escape_component(worker)}:{pid}:{start_usec}")
