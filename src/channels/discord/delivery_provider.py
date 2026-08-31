#!/usr/bin/env python3
"""DiscordDeliveryProvider: binds the shared DiscordRestClient into the
#3013 delivery-core seam — the first production provider behind it.

The provider owns only translation: payload bytes -> one Discord send,
transport receipt -> contract receipt. Retry/UNKNOWN policy stays in
DeliveryCore (its normative rules), single-attempt semantics stay in the
client. A malformed payload RAISES — a config error must never masquerade
as delivery ambiguity (the contract's typed-failure rule).

Payload schema (JSON bytes): {"channel_id": "...", "content": "..."}.
File attachments arrive with the poll_proactive conversion (5b).
"""
from __future__ import annotations

import json

from ag2_sparrow.delivery_core.contract import (
    DeliveryAttempt, DeliveryOutcome, DeliveryReceipt, ProviderCapabilities)
from channels.discord.client import DiscordRestClient
from outbox import DeliveryOutcome as TransportOutcome

_OUTCOME_MAP = {
    TransportOutcome.CONFIRMED: DeliveryOutcome.CONFIRMED,
    TransportOutcome.NOT_DELIVERED: DeliveryOutcome.NOT_DELIVERED,
    TransportOutcome.OUTCOME_UNKNOWN: DeliveryOutcome.OUTCOME_UNKNOWN,
}
# Totality is load-bearing: a miss in deliver() raises AFTER the send, leaking
# the claim into a redelivery (capabilities declare no reconcile/dedupe here).
assert set(_OUTCOME_MAP) == set(TransportOutcome)


class DiscordDeliveryProvider:
    """Discord has no queryable receipt store and no key-deduped send, so
    both post-UNKNOWN capabilities are off: the core parks UNKNOWN."""

    capabilities = ProviderCapabilities(reconcile_capable=False,
                                        idempotent_send=False)

    def __init__(self, client: DiscordRestClient):
        self._client = client

    def deliver(self, item_id: str, payload: bytes,
                idempotency_key: str) -> DeliveryReceipt:
        req = json.loads(payload.decode("utf-8"))
        channel_id = req["channel_id"]          # KeyError propagates: config error
        transport = self._client.send_message(channel_id,
                                              {"content": req["content"]})
        return DeliveryReceipt(
            outcome=_OUTCOME_MAP[transport.outcome],
            provider_ref=transport.receipt_id,
            detail=transport.detail,
        )

    def reconcile(self, attempt: DeliveryAttempt):
        return None  # capability-gated off; the core never calls this
