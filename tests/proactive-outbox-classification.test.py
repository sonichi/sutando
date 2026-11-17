#!/usr/bin/env python3
"""The proactive path decides delivery through the outbox, not a local boolean.

_post_proactive used `event_id present OR (PROACTIVE_TRUST_OK and ok is True)`.
The second half is the problem: with the flag on, a bare `{"ok": true}` counts as
delivered and the item is archived, so a send that may never have arrived is
recorded as one that did. There is no later signal that would correct it.

The send now goes through the outbox's room-message provider, which confirms only on
a Matrix event id; a 2xx without one is OUTCOME_UNKNOWN, parked and never resent.

Run: python3 tests/proactive-outbox-classification.test.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.outbox import DeliveryOutcome            # noqa: E402
from ag2_sparrow.outbox_adapter import classify_response  # noqa: E402

FAILS: list[str] = []


def check(title: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {title}")
    else:
        FAILS.append(title)
        print(f"  FAIL {title}\n         {detail}")


def check_call(title: str, fn, predicate, detail: str = "") -> None:
    """check() for a probe that may RAISE. Without this a raise ends the run and
    the remaining checks never execute, which reads short rather than broken."""
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001
        FAILS.append(title)
        print(f"  ERR  {title}\n         {type(exc).__name__}: {exc}")
        return
    check(title, predicate(got), detail)


def main() -> int:
    # 1 — the live gateway reply is not proof
    r = classify_response(200, {"ok": True})
    check("bare ok is OUTCOME_UNKNOWN",
          r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN,
          f"got {r.outcome}; archiving on this records an unproven send as delivered")

    # 2 — an identifier is
    r2 = classify_response(200, {"ok": True, "event_id": "$e"})
    check("an event id is CONFIRMED",
          r2.outcome is DeliveryOutcome.CONFIRMED and r2.receipt_id == "$e",
          f"got {r2.outcome} / {r2.receipt_id!r}")

    # 3 — the bridge has no trust flag, and its proactive send is the outbox provider's
    src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text(encoding="utf-8")
    check("the bridge reads no trust-ok setting",
          "PROACTIVE_TRUST_OK" not in src,
          "a bare ok can again be trusted as delivered")
    provider = (REPO / "packages/ag2-sparrow/ag2_sparrow/delivery_core/provider_ag2space.py").read_text(encoding="utf-8")
    check("the room-message provider pins the accepted identifier to event_id",
          'id_keys=("event_id",)' in provider,
          "the provider takes the default key set, so ts/id/message_id would "
          "confirm a proactive send")
    check("the proactive lane is built on the room-message provider",
          "AG2SpaceRoomMessageProvider(lambda *a: _post_proactive_message(*a)" in src,
          "the proactive send no longer goes through the outbox provider")

    # 4 — the gateway reports failure inside a 200 body. Only the key set can tell.
    for shape in ({"ok": True, "ts": "170099"},
                  {"errcode": "M_LIMIT_EXCEEDED", "id": "req-9"},
                  {"ok": False, "message_id": "m1"}):
        check_call(f"still unconfirmed at the bridge's key set: {shape}",
                   lambda s=shape: classify_response(200, s, id_keys=("event_id",)),
                   lambda r: r.outcome is DeliveryOutcome.OUTCOME_UNKNOWN,
                   "a gateway reply that delivered nothing would be archived as "
                   "delivered, and nothing later corrects it")

    print(f"\n  {len(FAILS)} failure(s)")
    if FAILS:
        print("\nFAILED")
        return 1
    print("\nPASS — proactive delivery is decided by the outbox receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
