"""Idempotent gateway result submission: acceptance closes the broker lease.

A successful POST confirms gateway acceptance, not Matrix delivery. The gateway
uses the envelope's result ID for deduplication; retries preserve that ID.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Callable, Optional

from ..outbox import DeliveryOutcome as OutboxOutcome
from ..outbox_adapter import classify_response
from ..send_failure_policy import is_retryable_http_status

from .contract import (DeliveryAttempt, DeliveryOutcome, DeliveryReceipt,
                       ProviderCapabilities, ProviderIndeterminate,
                       ProviderRefused, ProviderPermanentRefused)

RESULTS_PATH = "/v1/results"
# Optional result field (docs/remote-gateway-protocol.md): only this value is sent;
# a 400 for it is a bug to investigate and parks like any other 4xx.
RESULT_THREAD_FIELD, RESULT_THREAD_ASK = "thread", "ask"


class AG2SpaceResultProvider:
    """``request``: ``(method, path, payload_dict) -> parsed-json dict``,
    raising urllib errors on failure — the bridge's ``_req`` signature."""

    capabilities = ProviderCapabilities(reconcile_capable=False,
                                        idempotent_send=True)

    def __init__(self, request: Callable[..., dict]):
        self._request = request

    def deliver(self, item_id: str, payload: bytes,
                idempotency_key: str) -> DeliveryReceipt:
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            # Nothing was sent; a malformed envelope must not burn retries
            # as if the gateway had refused it — but it maps the same way.
            raise ProviderPermanentRefused(f"malformed envelope for {item_id}: {e}") from e
        if not isinstance(envelope, dict) or not envelope.get("id"):
            raise ProviderPermanentRefused(f"envelope for {item_id} lacks a result id")
        try:
            resp = self._request("POST", RESULTS_PATH, envelope) or {}
        except urllib.error.HTTPError as e:
            # The bridge refreshes credentials through its polling loop.
            if not is_retryable_http_status(e.code, auth_recoverable=True):
                raise ProviderPermanentRefused(
                    f"gateway refused {item_id}: HTTP {e.code}") from e
            raise ProviderIndeterminate(
                f"gateway retryable response for {item_id}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # urllib can't separate connect-refused from response-lost-
            # after-send; the idempotent re-send resolves the ambiguity.
            raise ProviderIndeterminate(
                f"transport failure for {item_id}: {e}") from e
        # This receipt confirms acceptance only, never downstream Matrix delivery.
        if resp.get("ok") is False or resp.get("error") or resp.get("errcode"):
            raise ProviderPermanentRefused(f"gateway declined {item_id}: {str(resp)[:200]}")
        return DeliveryReceipt(
            outcome=DeliveryOutcome.CONFIRMED,
            provider_ref="duplicate" if resp.get("duplicate") else "accepted",
            detail="gateway accepted; Matrix delivery unconfirmed")

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        # Declines to answer: the gateway exposes no read-back, so ambiguity is
        # resolved by this provider's idempotent re-send, not by reconciliation.
        return None


def room_response_refused(resp) -> bool:
    """An explicit refusal in a room-op response wins over any id the same response carries."""
    return isinstance(resp, dict) and bool(resp.get("ok") is False or resp.get("error")
                                           or resp.get("errcode"))


class AG2SpaceRoomMessageProvider:
    """Sends one message to an AG2 Space room through the injected room sender. Only a Matrix
    event id proves delivery, and it is durably recorded before the send counts as delivered."""

    capabilities = ProviderCapabilities(reconcile_capable=False, idempotent_send=False)

    def __init__(self, send: Callable[[str, str, str], dict],
                 record_receipt: Callable[[str, str], None]):
        self._send = send
        self._record_receipt = record_receipt

    def deliver(self, item_id: str, payload: bytes,
                idempotency_key: str) -> DeliveryReceipt:
        try:
            message = json.loads(payload.decode("utf-8"))
            room, body = message["room_id"], message["body"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as e:
            raise ProviderPermanentRefused(f"malformed room message for {item_id}: {e}") from e
        try:
            resp = self._send(room, body, idempotency_key)
        except urllib.error.HTTPError as e:
            if 500 <= e.code <= 599:
                raise ProviderIndeterminate(f"gateway error for {item_id}: HTTP {e.code}") from e
            if is_retryable_http_status(e.code, auth_recoverable=True):
                raise ProviderRefused(f"gateway refused {item_id} for now: HTTP {e.code}") from e
            raise ProviderPermanentRefused(f"gateway refused {item_id}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderIndeterminate(f"transport failure for {item_id}: {e}") from e
        if room_response_refused(resp):
            raise ProviderPermanentRefused(f"gateway declined {item_id}: {str(resp)[:200]}")
        receipt = classify_response(200, resp, id_keys=("event_id",))
        event_id = resp.get("event_id") if isinstance(resp, dict) else None
        if not (receipt.outcome is OutboxOutcome.CONFIRMED and isinstance(event_id, str)
                and event_id.startswith("$") and len(event_id.strip()) > 1):
            raise ProviderIndeterminate(f"accepted without a Matrix event id for {item_id}")
        try:
            self._record_receipt(item_id, event_id)
        except Exception as e:  # noqa: BLE001 — sent, but not provably recorded as sent
            raise ProviderIndeterminate(f"confirmed {item_id} but its receipt was not recorded: {e}") from e
        return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED, provider_ref=event_id,
                               destination=room, detail=receipt.detail)

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        return None
