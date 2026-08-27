#!/usr/bin/env python3
"""AG2SpaceResultProvider contract — classification of every gateway answer
shape onto the three-state receipt taxonomy, and the capability the provider
exists for: OUTCOME_UNKNOWN resolved by an idempotent re-send whose
rid-deduped answer (duplicate:true) confirms the FIRST attempt landed.

Run: python3 tests/ag2space-provider.test.py"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import io
import sys
import tempfile
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import json

from ag2_sparrow.delivery_core import (DeliveryAttempt, DeliveryCore,
                                       DeliveryOutcome, DesignAClaimBackend,
                                       DrainStatus, ProviderIndeterminate,
                                       ProviderRefused, RetryPolicy)
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


class ScriptedGateway:
    """Request double: pops one scripted answer per call. An answer may be an
    exception instance (raised) or a dict (returned)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b""))


ENVELOPE = json.dumps({"id": "task-X", "body": "hello"}).encode()


def outcome_of(answer):
    gw = ScriptedGateway(answer)
    p = AG2SpaceResultProvider(gw)
    try:
        return p.deliver("task-X", ENVELOPE, "task-X#0"), gw
    except (ProviderRefused, ProviderIndeterminate) as e:
        return e, gw


def main() -> int:
    # -- answer-shape classification -------------------------------------
    r, gw = outcome_of({"ok": True})
    check("2xx ok -> CONFIRMED/accepted",
          r.outcome is DeliveryOutcome.CONFIRMED and r.provider_ref == "accepted")
    check("wire shape: POST /v1/results with the envelope verbatim",
          gw.calls == [("POST", "/v1/results", {"id": "task-X", "body": "hello"})])

    r, _ = outcome_of({"ok": True, "duplicate": True})
    check("rid-deduped resend -> CONFIRMED/duplicate",
          r.outcome is DeliveryOutcome.CONFIRMED and r.provider_ref == "duplicate")

    r, _ = outcome_of({})
    check("legacy empty 200 -> CONFIRMED (accept IS confirmation here)",
          getattr(r, "outcome", None) is DeliveryOutcome.CONFIRMED)

    r, _ = outcome_of({"ok": False})
    check("explicit ok:false -> Refused", isinstance(r, ProviderRefused))
    r, _ = outcome_of({"error": "nope"})
    check("explicit error envelope -> Refused", isinstance(r, ProviderRefused))

    r, _ = outcome_of(_http_error(400))
    check("HTTP 400 -> Refused (no side effect performed)",
          isinstance(r, ProviderRefused))
    r, _ = outcome_of(_http_error(500))
    check("HTTP 500 -> Indeterminate (may have crossed the boundary)",
          isinstance(r, ProviderIndeterminate))
    r, _ = outcome_of(urllib.error.URLError("refused"))
    check("URLError -> Indeterminate", isinstance(r, ProviderIndeterminate))
    r, _ = outcome_of(TimeoutError("t"))
    check("timeout -> Indeterminate", isinstance(r, ProviderIndeterminate))

    gw = ScriptedGateway({"ok": True})
    p = AG2SpaceResultProvider(gw)
    try:
        p.deliver("task-X", b"\xff not json", "task-X#0")
        check("malformed envelope refused", False)
    except ProviderRefused:
        check("malformed envelope refused", True)
    check("malformed envelope never reaches the gateway", gw.calls == [])

    check("capabilities: idempotent_send licensed by rid-dedup, no reconcile",
          p.capabilities.idempotent_send and not p.capabilities.reconcile_capable)
    check("reconcile answers None (no receipt-read endpoint)",
          p.reconcile(DeliveryAttempt("task-X", b"{}", "task-X#0")) is None)

    # Timeout on the send, duplicate:true on the idempotent re-send: the
    # FIRST attempt landed; the resend's dedup answer confirms it.
    with tempfile.TemporaryDirectory() as td:
        gw = ScriptedGateway(TimeoutError("response lost"),
                             {"ok": True, "duplicate": True})
        core = DeliveryCore(DesignAClaimBackend(Path(td)),
                            AG2SpaceResultProvider(gw),
                            policy=RetryPolicy(max_attempts=10), worker="w")
        core.backend.publish("task-X", ENVELOPE)
        res = core.deliver_one("task-X", ENVELOPE)
        check("UNKNOWN resolved by idempotent re-send -> CONFIRMED",
              res.status is DrainStatus.ATTEMPTED
              and res.outcome is DeliveryOutcome.CONFIRMED)
        check("exactly two gateway calls (send + safe resend)", len(gw.calls) == 2)

    # Double-UNKNOWN completes retryable under the idempotent-send license —
    # parking would strand an item a later safe re-send could still deliver.
    with tempfile.TemporaryDirectory() as td:
        gw = ScriptedGateway(TimeoutError("t1"), TimeoutError("t2"),
                             {"ok": True, "duplicate": True})
        core = DeliveryCore(DesignAClaimBackend(Path(td)),
                            AG2SpaceResultProvider(gw),
                            policy=RetryPolicy(max_attempts=10), worker="w")
        core.backend.publish("task-X", ENVELOPE)
        res = core.deliver_one("task-X", ENVELOPE)
        check("double-UNKNOWN completes NOT_DELIVERED (retryable), not parked",
              res.outcome is DeliveryOutcome.NOT_DELIVERED)
        res = core.deliver_one("task-X", ENVELOPE)
        check("next pass re-claims and the rid-dedup answer confirms",
              res.outcome is DeliveryOutcome.CONFIRMED)

    # A DELIVERED id is a completed lifecycle, not a live item: the same
    # broker tid can carry a later, distinct send (fresh cycle, C-parity).
    with tempfile.TemporaryDirectory() as td:
        gw = ScriptedGateway({"ok": True}, {"ok": True})
        core = DeliveryCore(DesignAClaimBackend(Path(td)),
                            AG2SpaceResultProvider(gw),
                            policy=RetryPolicy(max_attempts=10), worker="w")
        core.backend.publish("task-X", ENVELOPE)
        check("first cycle confirms",
              core.deliver_one("task-X", ENVELOPE).outcome
              is DeliveryOutcome.CONFIRMED)
        check("re-publish after DELIVERED starts a fresh cycle",
              core.backend.publish("task-X", ENVELOPE) is True)
        check("fresh cycle delivers independently",
              core.deliver_one("task-X", ENVELOPE).outcome
              is DeliveryOutcome.CONFIRMED)
        core.backend.publish("task-Y", ENVELOPE)
        t = core.backend.claim("task-Y", "w")
        core.backend.complete(t, DeliveryOutcome.OUTCOME_UNKNOWN)
        # UNKNOWN park is unreachable for THIS provider, planted directly:
        check("PARKED still refuses re-publish (operator holds it)",
              core.backend.publish("task-Y", ENVELOPE) is False)

    # Repeated refusals PARK at the ceiling. A retry that never terminates is a
    # duplicate generator (#2959/#2960), so the cap is not optional here.
    with tempfile.TemporaryDirectory() as td:
        gw = ScriptedGateway(*([_http_error(400)] * 5 + [{"ok": True}]))
        core = DeliveryCore(DesignAClaimBackend(Path(td)),
                            AG2SpaceResultProvider(gw),
                            policy=RetryPolicy(max_attempts=3), worker="w")
        core.backend.publish("task-X", ENVELOPE)
        for i in range(3):
            res = core.deliver_one("task-X", ENVELOPE)
            check(f"refusal #{i+1} completes NOT_DELIVERED",
                  res.outcome is DeliveryOutcome.NOT_DELIVERED
                  and core.backend.attempts("task-X") == i + 1)
        # The 4th pass must NOT reach the provider: the item is parked, so the
        # still-scripted {"ok": True} is never consumed.
        res = core.deliver_one("task-X", ENVELOPE)
        check("at the ceiling the item is PARKED, not retried forever",
              res.status is DrainStatus.NOT_CLAIMED)
        check("a parked item refuses re-publish (operator holds it)",
              core.backend.publish("task-X", ENVELOPE) is False)

    # The ceiling cannot be removed: None/0/negative are rejected at construction.
    for bad in (None, 0, -1):
        try:
            RetryPolicy(max_attempts=bad)
            check(f"RetryPolicy(max_attempts={bad!r}) must raise", False)
        except ValueError:
            check(f"RetryPolicy(max_attempts={bad!r}) rejected", True)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: AG2SpaceResultProvider — classification, idempotent-resend "
          "confirmation, mandatory retry ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
