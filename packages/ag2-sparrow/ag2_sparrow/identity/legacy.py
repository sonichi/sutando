"""R3 adapter: map pre-B shipped artifacts to explicit identities.

Each mapping is a pure function of the artifact's stable content — the same
artifact yields the same identities on every read, on every host. The shapes
covered are the census's shipped approximations; nothing here renames or
rewrites the artifacts themselves.
"""
from __future__ import annotations

from dataclasses import dataclass

from .derive import escape_component, idempotency_key, legacy_delivery_id
from .types import DeliveryId, IdempotencyKey, TaskId


@dataclass(frozen=True)
class LegacyMapping:
    delivery_id: DeliveryId
    idempotency_key: IdempotencyKey


def from_outbox_item(item_id: str, boundary: str) -> LegacyMapping:
    """An outbox record whose item_id functions as the delivery identity.
    Where item_id == task_id (the documented conflation), the key equals
    idempotency_key(task, boundary); a non-task item_id (e.g. the claim
    fence's filename#mtime_ns) maps by the same pure rule on its content."""
    return LegacyMapping(
        delivery_id=legacy_delivery_id(item_id, boundary),
        idempotency_key=IdempotencyKey(
            f"e:{escape_component(item_id)}@{escape_component(boundary)}"),
    )


def from_delivered_sentinel(task_id: str, boundary: str) -> LegacyMapping:
    """A task-keyed delivered-sentinel: delivery and side-effect were both
    keyed by the task (delivery_id == task_id debt, legal under R3)."""
    task = TaskId(task_id)
    return LegacyMapping(
        delivery_id=legacy_delivery_id(task.value, boundary),
        idempotency_key=idempotency_key(task, boundary),
    )
