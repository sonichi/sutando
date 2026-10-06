"""AG2SpaceResultProvider: the gateway /v1/results leg behind the
DeliveryProvider seam.

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

import json
import time
import urllib.error
from urllib.parse import quote
from typing import Callable, Optional

from .contract import (DeliveryAttempt, DeliveryOutcome, DeliveryReceipt,
                       ProviderCapabilities, ProviderIndeterminate,
                       ProviderRefused)

RESULTS_PATH = "/v1/results"
DELIVERY_PATH = "/v1/results/{}/delivery"
# Pauses between delivery reads after an accept (~6s total): long enough for
# a permanent send failure, which the broker decides within a second.
CONFIRM_WAITS = (0.25, 0.5, 1.0, 2.0, 2.0)


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

    def _settled(self, rid: str) -> Optional[dict]:
        """The broker's terminal delivery state for `rid`, or None when it
        did not settle in the window or cannot be read."""
        for pause in (0.0, *self._confirm_waits):
            if pause:
                self._sleep(pause)
            try:
                st = self._request(
                    "GET", DELIVERY_PATH.format(quote(rid, safe="")), None)
            except (urllib.error.URLError, OSError, ValueError):
                return None     # includes 404: an older broker, or untracked
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
            raise ProviderRefused(f"malformed envelope for {item_id}: {e}") from e
        if not isinstance(envelope, dict) or not envelope.get("id"):
            raise ProviderRefused(f"envelope for {item_id} lacks a result id")
        try:
            resp = self._request("POST", RESULTS_PATH, envelope) or {}
        except urllib.error.HTTPError as e:
            # 4xx = rejected before the effect; 5xx may have crossed it.
            if 400 <= e.code < 500:
                raise ProviderRefused(
                    f"gateway refused {item_id}: HTTP {e.code}") from e
            raise ProviderIndeterminate(
                f"gateway 5xx for {item_id}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # urllib can't separate connect-refused from response-lost-
            # after-send; the idempotent re-send resolves the ambiguity.
            raise ProviderIndeterminate(
                f"transport failure for {item_id}: {e}") from e
        # Only an explicit decline envelope on a 2xx maps to refusal here.
        if resp.get("ok") is False or resp.get("error") or resp.get("errcode"):
            raise ProviderRefused(f"gateway declined {item_id}: {str(resp)[:200]}")
        ref = "duplicate" if resp.get("duplicate") else "accepted"
        if envelope.get("no_send") or not resp.get("delivery_readable"):
            return DeliveryReceipt(
                outcome=DeliveryOutcome.CONFIRMED, provider_ref=ref,
                detail="no_send" if envelope.get("no_send")
                else "delivery unconfirmed (broker exposes no delivery read)")
        st = self._settled(str(envelope["id"]))
        status = str((st or {}).get("status") or "")
        reason = str((st or {}).get("reason") or "")
        if status == "dead" and reason.startswith("send-failed"):
            # Re-posting is deduped by rid, so the retries this earns end in a park.
            raise ProviderRefused(
                f"broker dead-lettered {item_id} after accepting it: {reason}")
        if status == "delivered":
            return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED,
                                   provider_ref="delivered")
        return DeliveryReceipt(
            outcome=DeliveryOutcome.CONFIRMED, provider_ref=ref,
            detail=(f"delivery unconfirmed ({status or 'unsettled'})"
                    + ("; rid-deduped resend" if ref == "duplicate" else "")))

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        # Declines to answer: ambiguity is resolved by this provider's
        # idempotent re-send, not by reconciliation (see module doc).
        return None
