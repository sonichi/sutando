"""AG2SpaceResultProvider: the gateway /v1/results leg behind the
DeliveryProvider seam.

Transport-only by design: the adapter injects its authenticated request
callable (URL/token/User-Agent conventions live in ONE place, the bridge's
``_req``), and this provider owns classification — mapping the gateway's
responses onto the three-state receipt taxonomy.

idempotent_send is licensed by the gateway's rid-keyed done-window dedup,
live-verified on prod 2026-08-18: re-POSTing an already-recorded result id
returns ``{"ok": true, "duplicate": true}`` and does NOT re-deliver. An
OUTCOME_UNKNOWN may therefore safely re-send; a resend that finds the first
attempt landed comes back CONFIRMED via the duplicate flag instead of
double-posting to the room. reconcile stays undeclared: the gateway has no
receipt-read endpoint, and ``reconcile``'s signature carries no payload to
re-POST — the idempotent re-send IS this provider's reconciliation.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Callable, Optional

from .contract import (DeliveryAttempt, DeliveryOutcome, DeliveryReceipt,
                       is_declined_envelope,
                       ProviderCapabilities, ProviderIndeterminate,
                       ProviderRefused)

RESULTS_PATH = "/v1/results"


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
        # 2xx IS confirmation here (recorded + lease-closed; see class doc);
        # only an explicit decline envelope on a 2xx maps to refusal.
        if is_declined_envelope(resp):
            raise ProviderRefused(f"gateway declined {item_id}: {str(resp)[:200]}")
        return DeliveryReceipt(
            outcome=DeliveryOutcome.CONFIRMED,
            provider_ref="duplicate" if resp.get("duplicate") else "accepted",
            detail="rid-deduped resend" if resp.get("duplicate") else "")

    def reconcile(self, attempt: DeliveryAttempt) -> Optional[DeliveryReceipt]:
        # Declines to answer: the gateway exposes no read-back, so ambiguity is
        # resolved by this provider's idempotent re-send, not by reconciliation.
        return None
