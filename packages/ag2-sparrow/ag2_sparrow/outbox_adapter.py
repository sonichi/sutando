"""The Outbox's transport seam: turn a provider response into a DeliveryReceipt.

The core never sees a status code or a response body. It sees an outcome, and
decides claim / retry / park from that alone. Everything provider-specific stops
here, so a new transport is a new adapter rather than a change to retry policy.

Mapping, and the reasoning that fixes each side:

    explicit id            -> CONFIRMED       positive proof
    2xx with no id         -> OUTCOME_UNKNOWN accepted, unproven
    4xx                    -> NOT_DELIVERED   understood and refused
    5xx / timeout / raise  -> OUTCOME_UNKNOWN may have applied before failing

The 2xx-without-id row is the one that matters. Reading it as CONFIRMED archives
an item that may never have arrived; reading it as NOT_DELIVERED re-sends one
that did. Only the third state is honest, and the core is built to park on it.

Contracts: tests/outbox-adapter-contract.test.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .outbox import DeliveryOutcome, RetrySafety

# Keys providers use for "here is the thing I created". Order is preference.
_ID_KEYS = ("event_id", "message_id", "id", "ts")


@dataclass(frozen=True)
class DeliveryReceipt:
    """What the core is allowed to know about a send."""
    outcome: DeliveryOutcome
    receipt_id: Optional[str] = None
    safety: RetrySafety = RetrySafety.UNSAFE
    detail: str = ""


def classify_response(status: Optional[int], body: Any) -> DeliveryReceipt:
    """Provider response -> receipt. Pure; no I/O, no policy."""
    rid = None
    if isinstance(body, dict):
        for k in _ID_KEYS:
            v = body.get(k)
            if isinstance(v, str) and v:
                rid = v
                break
    if rid:
        return DeliveryReceipt(DeliveryOutcome.CONFIRMED, receipt_id=rid,
                               detail="provider returned an identifier")
    if status is None:
        return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                               detail="no response (timeout or transport failure)")
    if 200 <= status < 300:
        # Accepted, unproven. The honest state, and the reason this seam exists.
        return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                               detail="accepted without an identifier")
    if 400 <= status < 500:
        return DeliveryReceipt(DeliveryOutcome.NOT_DELIVERED,
                               detail="refused by the provider")
    return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                           detail="provider error; the write may have applied")


class DeliveryAdapter:
    """Base transport seam. Subclasses implement `_transmit` and nothing else.

    Deliberately exposes no retry, attempt, or backoff surface: an adapter that
    retries privately is invisible to the core's attempt budget and unbounded by
    it, which is the failure this design removes.
    """

    def _transmit(self, item: dict) -> tuple[Optional[int], Any]:
        """-> (status, body). Provider I/O only."""
        raise NotImplementedError

    def send(self, item: dict) -> DeliveryReceipt:
        try:
            status, body = self._transmit(item)
        except Exception as exc:  # noqa: BLE001
            # A raise mid-write is the case where the peer may already have
            # processed the request; NOT_DELIVERED here would re-send it.
            return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                                   detail=f"{type(exc).__name__}: {exc}")
        return classify_response(status, body)


class AG2SpaceAdapter(DeliveryAdapter):
    """Posts an outbound item to an AG2 Space room via the gateway.

    `poster` is injected so the transport stays at the edge and the seam is
    testable without a live gateway.
    """

    def __init__(self, poster, room_id: str):
        self._poster = poster
        self._room_id = room_id

    def _transmit(self, item: dict) -> tuple[Optional[int], Any]:
        return self._poster(self._room_id, item.get("body", ""))
