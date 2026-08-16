#!/usr/bin/env python3
"""The proactive path decides delivery through the outbox, not a local boolean.

_post_proactive used `event_id present OR (PROACTIVE_TRUST_OK and ok is True)`.
The second half is the problem: with the flag on, a bare `{"ok": true}` counts as
delivered and the item is archived, so a send that may never have arrived is
recorded as one that did. There is no later signal that would correct it.

Routing the same response through classify_response yields CONFIRMED only on an
identifier; a 2xx without one is OUTCOME_UNKNOWN, which the existing
send_failure_policy path already handles with a bounded retry.

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

    # 3 — the decision in the bridge must not read the trust flag
    src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text(encoding="utf-8")
    m = re.search(r"receipt = classify_response\(.*?\n\s*delivered = (.*)", src)
    check("the bridge derives `delivered` from the receipt",
          bool(m) and "CONFIRMED" in m.group(1),
          "delivered is no longer computed from the outbox receipt")
    # The receipt reports what the response proves; the flag chooses what to do
    # when it proves nothing. Separate clauses, never folded.
    check("the trust opt-in survives as its own clause",
          "PROACTIVE_TRUST_OK and isinstance(resp, dict)" in src,
          "the at-least-once opt-in was removed; that is a documented, tested "
          "feature, not a fallback")

    print(f"\n  {len(FAILS)} failure(s)")
    if FAILS:
        print("\nFAILED")
        return 1
    print("\nPASS — proactive delivery is decided by the outbox receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
