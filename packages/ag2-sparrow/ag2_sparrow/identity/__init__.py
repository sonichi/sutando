"""Sparrow delivery identity — the B1 contract (slice 2 of the B sequence).

Canonical constructors for the five identities frozen in
docs/sparrow-delivery-identity.md. src/ code must obtain delivery identities
from here, never derive its own (the identity ratchet pins this).
"""
from .derive import (attempt_id, delivery_id, escape_component,
                     idempotency_key, incarnation_id_from, ingress_task_id,
                     legacy_delivery_id, legacy_idempotency_key,
                     resend_delivery_id)
from .legacy import LegacyMapping, from_delivered_sentinel, from_outbox_item
from .serialization import (identity_fields_from_record, parse_attempt_id,
                            parse_delivery_id, parse_idempotency_key,
                            parse_incarnation_id, parse_task_id,
                            to_record_fields)
from .types import (AttemptId, DeliveryId, IdempotencyKey, IncarnationId,
                    TaskId)

__all__ = [
    "AttemptId", "DeliveryId", "IdempotencyKey", "IncarnationId", "TaskId",
    "attempt_id", "delivery_id", "escape_component", "idempotency_key",
    "incarnation_id_from", "ingress_task_id", "legacy_delivery_id",
    "legacy_idempotency_key", "resend_delivery_id",
    "LegacyMapping", "from_delivered_sentinel", "from_outbox_item",
    "identity_fields_from_record", "parse_attempt_id", "parse_delivery_id",
    "parse_idempotency_key", "parse_incarnation_id", "parse_task_id",
    "to_record_fields",
]
