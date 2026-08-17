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
from typing import Any, Optional, Sequence

try:  # vendored into ag2_sparrow as a package module; flat in src/
    from .outbox import DeliveryOutcome, RetrySafety
except ImportError:  # pragma: no cover - exercised by whichever import wins
    from outbox import DeliveryOutcome, RetrySafety

# Keys providers use for "here is the thing I created". Order is preference.
# `ts` is excluded: here it is a send time, not a receipt.
_ID_KEYS = ("event_id", "message_id", "id")


@dataclass(frozen=True)
class DeliveryReceipt:
    """What the core is allowed to know about a send."""
    outcome: DeliveryOutcome
    receipt_id: Optional[str] = None
    safety: RetrySafety = RetrySafety.UNSAFE
    detail: str = ""


def classify_response(status: Optional[int], body: Any,
                      id_keys: Sequence[str] = _ID_KEYS) -> DeliveryReceipt:
    """Provider response -> receipt. Pure; no I/O, no policy.

    `id_keys` narrows what counts as proof. A caller whose provider names its
    receipt must pin it, or the broad default widens what that path accepts.
    """
    # Status decides first: an error envelope's `id` is a trace id, and reading
    # it as a receipt archives an item the provider just refused.
    if status is None:
        return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                               detail="no response (timeout or transport failure)")
    if 400 <= status < 500:
        return DeliveryReceipt(DeliveryOutcome.NOT_DELIVERED,
                               detail="refused by the provider")
    if not (200 <= status < 300):
        return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                               detail="provider error; the write may have applied")
    rid = None
    if isinstance(body, dict):
        for k in id_keys:
            v = body.get(k)
            # bool is an int subclass; `True` is a flag, never an identifier.
            if isinstance(v, bool):
                continue
            if isinstance(v, (str, int)) and str(v).strip():
                rid = str(v)
                break
    if rid:
        return DeliveryReceipt(DeliveryOutcome.CONFIRMED, receipt_id=rid,
                               detail="provider returned an identifier")
    # Accepted, unproven. The honest state, and the reason this seam exists.
    return DeliveryReceipt(DeliveryOutcome.OUTCOME_UNKNOWN,
                           detail="accepted without an identifier")


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
            # The peer may already have processed it; NOT_DELIVERED re-sends.
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
