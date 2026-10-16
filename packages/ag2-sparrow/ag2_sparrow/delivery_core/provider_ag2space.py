"""Idempotent gateway result submission: acceptance closes the broker lease.

Transport-only by design: the adapter injects its authenticated request
callable (URL/token/User-Agent conventions live in ONE place, the bridge's
``_req``), and this provider owns classification — mapping the gateway's
responses onto the three-state receipt taxonomy.

A 2xx means the broker RECORDED the result; its Matrix send is asynchronous
and can still dead-letter. When the accept says ``delivery_readable``, the
provider reads ``GET /v1/results/<id>/delivery`` for a bounded window:
``delivered`` confirms (provider_ref ``delivered``), a ``dead`` send failure
is a refusal (the message never reached the room), and anything else — still
sending, no delivery state, an unreadable answer — stays CONFIRMED on the
accept with provider_ref ``accepted``/``duplicate``: recorded, delivery
unconfirmed. An accept without the flag (an older broker) is exactly that,
with no read at all.

idempotent_send is licensed by the gateway's rid-keyed done-window dedup,
live-verified on prod 2026-08-18: re-POSTing an already-recorded result id
returns ``{"ok": true, "duplicate": true}`` and does NOT re-deliver. An
OUTCOME_UNKNOWN may therefore safely re-send; a resend that finds the first
attempt landed comes back CONFIRMED via the duplicate flag instead of
double-posting to the room. reconcile stays undeclared: the delivery read
answers only for a result the broker recorded, so it cannot resolve a POST
that may never have arrived — the idempotent re-send IS this provider's
reconciliation (and runs the same delivery read).
"""
from __future__ import annotations

import http.client
import json
from collections import OrderedDict
import time
import urllib.error
from urllib.parse import quote
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
DELIVERY_PATH = "/v1/results/{}/delivery"
# Pauses between delivery reads after an accept (~6s total): long enough for
# a permanent send failure, which the broker decides within a second.
CONFIRM_WAITS = (0.25, 0.5, 1.0, 2.0, 2.0)


# Readable send failures remembered per process; older rids are parked by then.
DEAD_MEMORY_LIMIT = 512


class AG2SpaceResultProvider:
    """``request``: ``(method, path, payload_dict) -> parsed-json dict``,
    raising urllib errors on failure — the bridge's ``_req`` signature."""

    capabilities = ProviderCapabilities(reconcile_capable=False,
                                        idempotent_send=True)

    def __init__(self, request: Callable[..., dict], *,
                 confirm_waits=CONFIRM_WAITS,
                 sleep: Callable[[float], None] = time.sleep):
        self._request = request
        self._confirm_waits = tuple(confirm_waits)
        self._sleep = sleep
        # rid -> reason for each readable send failure seen here, newest last, capped:
        # an unreadable later poll must not turn a known dead-letter into a delivery.
        self._dead: OrderedDict = OrderedDict()

    def _settled(self, rid: str) -> Optional[dict]:
        """The broker's terminal delivery state for `rid`, or None when it
        did not settle in the window or cannot be read."""
        for pause in (0.0, *self._confirm_waits):
            if pause:
                self._sleep(pause)
            try:
                st = self._request(
                    "GET", DELIVERY_PATH.format(quote(rid, safe="")), None)
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
                return None     # includes 404 and a truncated read: nothing readable
            if not isinstance(st, dict) or not st.get("status"):
                return None     # not a delivery answer: nothing to wait for
            if st.get("terminal"):
                return st
        return None

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
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
            # urllib can't separate connect-refused from response-lost-
            # after-send; the idempotent re-send resolves the ambiguity.
            raise ProviderIndeterminate(
                f"transport failure for {item_id}: {e}") from e
        # A 2xx is the broker RECORDING the result, not its Matrix send; only an
        # explicit decline envelope on it is a refusal.
        if resp.get("ok") is False or resp.get("error") or resp.get("errcode"):
            raise ProviderPermanentRefused(f"gateway declined {item_id}: {str(resp)[:200]}")
        ref = "duplicate" if resp.get("duplicate") else "accepted"
        if envelope.get("no_send") or not resp.get("delivery_readable"):
            return DeliveryReceipt(
                outcome=DeliveryOutcome.CONFIRMED, provider_ref=ref,
                detail="no_send" if envelope.get("no_send")
                else "delivery unconfirmed (broker exposes no delivery read)")
        rid = str(envelope["id"])
        st = self._settled(rid)
        status = str((st or {}).get("status") or "")
        reason = str((st or {}).get("reason") or "")
        if status == "dead" and reason.startswith("send-failed"):
            # Re-posting is deduped by rid, so the retries this earns end in a park.
            self._dead[rid] = reason
            self._dead.move_to_end(rid)
            while len(self._dead) > DEAD_MEMORY_LIMIT:
                self._dead.popitem(last=False)
            raise ProviderRefused(
                f"broker dead-lettered {item_id} after accepting it: {reason}")
        if status == "delivered":
            self._dead.pop(rid, None)
            return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                                   provider_ref="delivered")
        if rid in self._dead:
            # Known non-delivery stays known: an unreadable or non-terminal poll after a
            # readable send failure is not evidence the broker sent it.
            raise ProviderRefused(
                f"broker dead-lettered {item_id} earlier and has not reported it delivered "
                f"since: {self._dead[rid]} (latest read: "
                f"{(status + ('/' + reason if reason else '')) or 'unreadable'})")
        return DeliveryReceipt(
            outcome=DeliveryOutcome.CONFIRMED, provider_ref=ref,
            detail=(f"delivery unconfirmed ({status or 'unsettled'})"
                    + ("; rid-deduped resend" if ref == "duplicate" else "")))

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        # Declines to answer: ambiguity is resolved by this provider's
        # idempotent re-send, not by reconciliation (see module doc).
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
