"""Idempotent gateway result submission: acceptance closes the broker lease.

A successful POST confirms gateway acceptance, not Matrix delivery. The gateway
uses the envelope's result ID for deduplication; retries preserve that ID.
"""
from __future__ import annotations

import json
import socket
import urllib.error
from typing import Callable, Optional

from ..outbox_adapter import classify_response
from ..send_failure_policy import is_retryable_http_status

from .contract import (DeliveryAttempt, DeliveryOutcome, DeliveryReceipt,
                       ProviderCapabilities, ProviderIndeterminate,
                       ProviderRefused, ProviderPermanentRefused)

RESULTS_PATH = "/v1/results"
ROOM_PATH = "/v1/room"


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


class AG2SpaceRoomMessageProvider:
    """One `op: message` room post, e.g. a task result inside a thread.

    `/v1/room` takes no idempotency key, so this send is at-most-once: whatever
    may have posted is OUTCOME_UNKNOWN, which the core parks and never re-sends.
    Only a send that provably never posted is NOT_DELIVERED."""

    capabilities = ProviderCapabilities(reconcile_capable=False,
                                        idempotent_send=False)

    def __init__(self, request: Callable[..., dict],
                 trust_ok: Callable[[], bool] = lambda: False):
        self._request = request
        self._trust_ok = trust_ok

    def deliver(self, item_id: str, payload: bytes,
                idempotency_key: str) -> DeliveryReceipt:
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ProviderPermanentRefused(f"malformed envelope for {item_id}: {e}") from e
        if not isinstance(envelope, dict) or envelope.get("op") != "message" \
                or not envelope.get("room_id") or not envelope.get("body"):
            raise ProviderPermanentRefused(f"envelope for {item_id} is not a room message")
        try:
            resp = self._request("POST", ROOM_PATH, envelope, timeout=15) or {}
        except urllib.error.HTTPError as e:
            if classify_response(e.code, None).outcome.name != "NOT_DELIVERED":
                raise ProviderIndeterminate(f"room post for {item_id}: HTTP {e.code}") from e
            if is_retryable_http_status(e.code, auth_recoverable=True):
                raise ProviderRefused(f"room post for {item_id} refused: HTTP {e.code}") from e
            raise ProviderPermanentRefused(f"room post for {item_id} refused: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            # No connection was made, so nothing was posted.
            if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
                raise ProviderRefused(f"room post for {item_id} not sent: {e.reason}") from e
            raise ProviderIndeterminate(f"transport failure for {item_id}: {e}") from e
        except (TimeoutError, OSError) as e:
            raise ProviderIndeterminate(f"transport failure for {item_id}: {e}") from e
        if not isinstance(resp, dict):
            raise ProviderIndeterminate(f"room post for {item_id}: unreadable response")
        if resp.get("ok") is False or resp.get("errcode"):
            raise ProviderPermanentRefused(f"gateway declined {item_id}: {str(resp)[:200]}")
        receipt = classify_response(200, resp, id_keys=("event_id",))
        if receipt.outcome.name == "CONFIRMED" or (
                self._trust_ok() and resp.get("ok") is True):
            return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                                   provider_ref=receipt.receipt_id,
                                   detail=receipt.detail,
                                   destination=str(envelope["room_id"]))
        raise ProviderIndeterminate(f"room post for {item_id}: {receipt.detail}")

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        return None
