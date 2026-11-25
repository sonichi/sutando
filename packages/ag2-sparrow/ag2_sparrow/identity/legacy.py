"""R3 adapter: map pre-B shipped artifacts to explicit identities.

Each mapping is a pure function of the artifact's stable content — the same
artifact yields the same identities on every read, on every host. The shapes
covered are the census's shipped approximations; nothing here renames or
rewrites the artifacts themselves.
"""
from __future__ import annotations

from dataclasses import dataclass

from .derive import idempotency_key, legacy_delivery_id, legacy_idempotency_key
from .types import DeliveryId, IdempotencyKey, TaskId


@dataclass(frozen=True)
class LegacyMapping:
    delivery_id: DeliveryId
    idempotency_key: IdempotencyKey


def from_outbox_item(item_id: str, boundary: str,
                     resend_epoch: int = 0) -> LegacyMapping:
    """An outbox record whose item_id functions as the delivery identity.

    The delivery_id is new naming and may be canonical, but the idempotency
    key is NOT ours to choose: delivery_core has already offered this item to
    a provider under <item_id>#<epoch>, so the mapping must reproduce that key
    or a parked item is re-offered under a name the provider cannot dedupe —
    one duplicate side effect per adopted item. boundary therefore does not
    enter the key: the shipped key never carried it.
    """
    return LegacyMapping(
        delivery_id=legacy_delivery_id(item_id, boundary),
        idempotency_key=legacy_idempotency_key(item_id, resend_epoch),
    )


def from_delivered_sentinel(task_id: str, boundary: str,
                            resend_epoch: int = 0) -> LegacyMapping:
    """A task-keyed delivered-sentinel: delivery and side-effect were both
    keyed by the task (delivery_id == task_id debt, legal under R3).

    Unlike an outbox item this artifact is a local file: no provider ever saw
    a key for it, so there is nothing to preserve and the canonical key
    applies."""
    task = TaskId(task_id)
    return LegacyMapping(
        delivery_id=legacy_delivery_id(task.value, boundary),
        idempotency_key=idempotency_key(task, boundary, resend_epoch),
    )
