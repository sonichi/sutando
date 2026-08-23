"""Slack reply-leg delivery state, bound to the shared outbox (Slack strangler).

The outbox owns claim, retry, idempotency, and the three-state outcome; the
bridge keeps chat_postMessage mechanics only. Receipt classification delegates
to outbox_adapter with Slack's receipt key (`ts`) pinned.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ag2_sparrow.delivery_core import DesignAClaimBackend
from ag2_sparrow.delivery_core.contract import ClaimToken, DeliveryOutcome

import outbox
from outbox import DeliveryOutcome as ReceiptOutcome
from outbox_adapter import DeliveryReceipt, classify_response

WORKER = "slack-results"
PARK_AT_ATTEMPTS = 5

# chat.postMessage's receipt IS the message `ts` — excluded from the adapter's
# default keys (elsewhere a send time), but in Slack it is the message id.
SLACK_ID_KEYS = ("ts",)

_BACKENDS: dict = {}


def result_backend(results_dir: Path) -> DesignAClaimBackend:
    """Per-root singleton; the root lives INSIDE results/ so any harness that
    redirects RESULTS_DIR is hermetic for free (discord-results precedent)."""
    root = Path(results_dir) / ".outbox-slack-results"
    b = _BACKENDS.get(root)
    if b is None:
        b = _BACKENDS[root] = DesignAClaimBackend(root)
    return b


def is_delivered(results_dir: Path, task_id: str) -> bool:
    """DELIVERED means the send landed but a crash preceded the archive; the
    caller finishes the archive and never re-sends."""
    root = result_backend(results_dir).root
    return outbox.item_status(root, task_id) == "DELIVERED"


def is_parked(results_dir: Path, task_id: str) -> bool:
    """PARKED is terminal for the bridge: the disposition is already durable
    in the outbox, so the caller archives the result pair instead of looping."""
    root = result_backend(results_dir).root
    return outbox.item_status(root, task_id) == "PARKED"


def claim_for_send(results_dir: Path, task_id: str) -> Optional[ClaimToken]:
    """Publish (one-slot, idempotent) then claim. None = someone else holds
    it or it just completed — the caller skips this pass, never re-sends."""
    b = result_backend(results_dir)
    st = outbox.item_status(b.root, task_id)
    if st == "DELIVERED" or st == "PARKED":
        return None
    b.publish(task_id, b"")
    return b.claim(task_id, WORKER)


def confirm(results_dir: Path, token: ClaimToken, destination: str) -> bool:
    """CONFIRMED terminal. The §7 audit row stays in the bridge's _send_reply
    (already the Slack audit choke point) — recording here would double it."""
    return result_backend(results_dir).complete(
        token, DeliveryOutcome.CONFIRMED,
        provider="slack", destination=destination)


def failed(results_dir: Path, token: ClaimToken) -> bool:
    """NOT_DELIVERED: re-readied for the next pass (Slack's refusal semantics
    were always retry); parks at the attempt cap so an unsendable reply cannot
    retry forever (duplicate-generator rule)."""
    return result_backend(results_dir).complete(
        token, DeliveryOutcome.NOT_DELIVERED,
        park_at_attempts=PARK_AT_ATTEMPTS)


def unknown(results_dir: Path, token: ClaimToken) -> bool:
    """OUTCOME_UNKNOWN: the send MAY have reached Slack — park, never
    auto-retry (at-most-once bias on ambiguity, per the outbox contract)."""
    return result_backend(results_dir).complete(
        token, DeliveryOutcome.OUTCOME_UNKNOWN)


def receipt_for_response(resp: Any) -> DeliveryReceipt:
    """A returned chat.postMessage response -> three-state receipt.

    Slack refuses at HTTP 200 with ok:false — a definite refusal the generic
    status mapping cannot see, so it is resolved before delegating.
    """
    status = getattr(resp, "status_code", None)
    body = getattr(resp, "data", None)
    if body is None and isinstance(resp, dict):
        body = resp
    body = body if isinstance(body, dict) else None
    # A held response means the HTTP leg completed; classify a status-less
    # body as accepted rather than as a transport failure.
    if status is None and body is not None:
        status = 200
    if body is not None and body.get("ok") is False and \
            status is not None and 200 <= int(status) < 300:
        return DeliveryReceipt(
            ReceiptOutcome.NOT_DELIVERED,
            detail=f"slack refused: {body.get('error', 'ok:false')}")
    return classify_response(status, body, SLACK_ID_KEYS)


def receipt_for_error(exc: BaseException) -> DeliveryReceipt:
    """A raised provider error -> receipt. SlackApiError carries the refusing
    response; anything without one is a transport failure (OUTCOME_UNKNOWN)."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        return receipt_for_response(resp)
    return classify_response(None, None, SLACK_ID_KEYS)


def receipt_for_send(delivered_ok: bool, response: Any = None,
                     error: Optional[BaseException] = None) -> DeliveryReceipt:
    """Bridge-observed send material -> three-state receipt.

    A provider error outranks the boolean (it says WHY); success classifies
    the last chunk's response; an attachment-only success is accept-is-confirm
    (files_upload_v2 returned); a failure with no provider material is a
    definite NOT_DELIVERED (nothing external is ambiguous).
    """
    if error is not None:
        return receipt_for_error(error)
    if delivered_ok:
        if response is not None:
            return receipt_for_response(response)
        return DeliveryReceipt(ReceiptOutcome.CONFIRMED,
                               detail="no text body; attachments delivered")
    return DeliveryReceipt(ReceiptOutcome.NOT_DELIVERED,
                           detail="send leg reported failure without a raise")


def settle(results_dir: Path, token: ClaimToken, receipt: DeliveryReceipt,
           destination: str) -> str:
    """Map an ATTEMPTED send's receipt to the outbox transition.

    -> "delivered" (archive now) | "retry" (keep the pair) | "parked"
    (terminal; the is_parked pre-check archives it on the next pass).
    """
    if receipt.outcome is ReceiptOutcome.CONFIRMED:
        confirm(results_dir, token, destination)
        return "delivered"
    if receipt.outcome is ReceiptOutcome.NOT_DELIVERED:
        failed(results_dir, token)
        return "parked" if is_parked(results_dir, token.item_id) else "retry"
    unknown(results_dir, token)
    return "parked"
