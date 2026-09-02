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
    e:<task>@<boundary>            idempotency key, resend_epoch 0
    e:<task>@<boundary>+r<n>       the key of re-send epoch n (a NEW effect)
    <item_id>#<epoch>              the pre-B provider key, preserved verbatim
"""
from __future__ import annotations

import hashlib
import re

from .types import (AttemptId, DeliveryId, IdempotencyKey, IncarnationId,
                    TaskId)

from .escape import escape_component  # noqa: F401  (re-exported)


def _require(value, want, name: str):
    if not isinstance(value, want):
        raise TypeError(f"{name} must be {want.__name__}, "
                        f"got {type(value).__name__}")


def _require_ordinal(ordinal, name: str) -> None:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError(f"{name} must be an int, "
                        f"got {type(ordinal).__name__}")
    if ordinal < 1:
        raise ValueError(f"{name} is 1-based")


def _require_epoch(resend_epoch) -> None:
    if isinstance(resend_epoch, bool) or not isinstance(resend_epoch, int):
        raise TypeError(f"resend_epoch must be an int, "
                        f"got {type(resend_epoch).__name__}")
    if resend_epoch < 0:
        raise ValueError("resend_epoch is 0-based")


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
    _require(task, TaskId, "task")
    return DeliveryId(f"d:{escape_component(task.value)}@"
                      f"{escape_component(boundary)}")


# Mirrors the outbox's _safe_key: past this, a readable prefix plus a digest
# of the RAW key — the prefix is lossy, the digest decides identity.
_LEGACY_READABLE = 80


def _bounded_legacy_component(content_key: str) -> str:
    esc = escape_component(content_key)
    if len(esc) <= _LEGACY_READABLE + 17:
        return esc
    out, n = [], 0
    for tok in re.findall(r"%[0-9A-F]{2}|.", esc):   # never split an escape
        if n + len(tok) > _LEGACY_READABLE:
            break
        out.append(tok)
        n += len(tok)
    digest = hashlib.sha256(content_key.encode("utf-8")).hexdigest()[:16]
    return f"{''.join(out)}.{digest}"


def legacy_delivery_id(content_key: str, boundary: str) -> DeliveryId:
    """R3 mapping for a pre-B artifact with no delivery_id: a pure function
    of its stable content, so the same artifact maps to the same id on every
    read, on every host. content_key is whatever stable identity the artifact
    already carries (task_id, filename#mtime_ns, ...). Long or escape-dense
    keys take the digest form so derived attempt/resend ids stay bounded."""
    return DeliveryId(f"legacy:{_bounded_legacy_component(content_key)}@"
                      f"{escape_component(boundary)}")


def resend_delivery_id(predecessor: DeliveryId, ordinal: int) -> DeliveryId:
    """Post-reconciliation re-send: a NEW delivery with lineage to its
    predecessor (R2's only minting row). Ordinal is 1-based per predecessor."""
    _require(predecessor, DeliveryId, "predecessor")
    _require_ordinal(ordinal, "resend ordinal")
    return DeliveryId(f"{predecessor.value}+r{ordinal}")


def attempt_id(delivery: DeliveryId, ordinal: int) -> AttemptId:
    """One physical try. Ordered per delivery, 1-based, never reused."""
    _require(delivery, DeliveryId, "delivery")
    _require_ordinal(ordinal, "attempt ordinal")
    return AttemptId(f"{delivery.value}#a{ordinal}")


def idempotency_key(task: TaskId, boundary: str, resend_epoch: int = 0
                    ) -> IdempotencyKey:
    """One external side-effect. Retries and crash recovery keep epoch 0's
    key; a post-reconciliation re-send is a NEW effect, so its epoch advances
    and the key changes (sparrow-delivery-identity: R2's minting row)."""
    _require(task, TaskId, "task")
    _require_epoch(resend_epoch)
    base = (f"e:{escape_component(task.value)}@"
            f"{escape_component(boundary)}")
    return IdempotencyKey(base if resend_epoch == 0 else f"{base}+r{resend_epoch}")


def legacy_idempotency_key(item_id: str, resend_epoch: int = 0
                           ) -> IdempotencyKey:
    """The provider key ALREADY SHIPPED by delivery_core — <item_id>#<epoch>,
    reproduced byte-for-byte. Components are NOT escaped: these bytes are what
    a provider has already seen, so re-deriving them is the whole point, and
    the shape is opaque rather than injective. Deliveries begun before the
    canonical key exists must keep this key, or the same side effect is
    re-offered under a name the provider cannot recognise as a duplicate."""
    # Every str is in the domain, the empty string included: the shipped
    # formatter accepted it and a published item must stay drainable.
    if not isinstance(item_id, str):
        raise TypeError(f"item_id must be str, got {type(item_id).__name__}")
    _require_epoch(resend_epoch)
    return IdempotencyKey(f"{item_id}#{resend_epoch}")


def incarnation_id_from(worker: str, pid: int, start_usec: int) -> IncarnationId:
    """Attribution identity for one process lifetime. The caller supplies the
    process material; nothing derived here may feed the constructors above."""
    for name, val in (("pid", pid), ("start_usec", start_usec)):
        if isinstance(val, bool) or not isinstance(val, int):
            raise TypeError(f"{name} must be an int, got {type(val).__name__}")
        if val < 0:
            raise ValueError(f"{name} must be non-negative")
    return IncarnationId(f"{escape_component(worker)}:{pid}:{start_usec}")
