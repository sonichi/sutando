#!/usr/bin/env python3
"""Send-leg of proactive text delivery through the shared DeliveryProvider.

Stage 1 of the poll_proactive migration (5b): the bridge KEEPS claim/retry
orchestration — the cross-bridge `.sending` protocol needs a migration fence
before claiming moves — and only the outbound text send goes behind the
provider, so every chunk earns a canonical receipt and an OUTCOME_UNKNOWN
is never blindly resent as if it had cleanly failed.

Receipt -> exception mapping (send_failure_policy speaks exceptions):

  OUTCOME_UNKNOWN -> send_failure_policy.UnconfirmedDelivery — transient
                     WITH CAP: the retry stays visible to the bridge's
                     attempt budget, bounded, then parks loudly.
  NOT_DELIVERED   -> ProviderSendFailed (permanent by default): Discord
                     answered and said no; a retry does not change a 4xx.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_failure_policy import UnconfirmedDelivery  # noqa: E402


class ProviderSendFailed(RuntimeError):
    """Discord refused a chunk (NOT_DELIVERED). `sent_chunks` counts the
    chunks confirmed before the refusal — the caller's progressed signal."""

    def __init__(self, detail: str, sent_chunks: int):
        self.sent_chunks = sent_chunks
        super().__init__(f"{detail} (after {sent_chunks} confirmed chunk(s))")


def deliver_text(provider, channel_id, text: str, item_id: str, chunker) -> int:
    """Deliver `text` to `channel_id` chunk-by-chunk through `provider`.

    Returns the number of chunks confirmed. Raises on the first chunk that
    is not CONFIRMED; the idempotency key is per-chunk so a caller's bounded
    retry re-offers the same chunk identity, never a new one."""
    from ag2_sparrow.delivery_core.contract import DeliveryOutcome as CO

    sent = 0
    for i, chunk in enumerate(chunker(text)):
        payload = json.dumps(
            {"channel_id": str(channel_id), "content": chunk}).encode()
        receipt = provider.deliver(item_id, payload, f"{item_id}#{i}")
        if receipt.outcome is CO.OUTCOME_UNKNOWN:
            raise UnconfirmedDelivery(
                f"chunk {i + 1} unconfirmed: {receipt.detail} "
                f"(after {sent} confirmed chunk(s))")
        if receipt.outcome is not CO.CONFIRMED:
            raise ProviderSendFailed(
                f"chunk {i + 1} refused: {receipt.detail}", sent)
        sent += 1
    return sent
