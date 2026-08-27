#!/usr/bin/env python3
"""discord_proactive_send.deliver_text: the proactive text send-leg's contract.

CONFIRMED chunks flow in order with per-chunk idempotency keys; the first
OUTCOME_UNKNOWN raises send_failure_policy.UnconfirmedDelivery (transient
WITH CAP — the bounded-retry class, never a silent park, never an unbounded
resend); the first NOT_DELIVERED raises ProviderSendFailed carrying how many
chunks were already confirmed (the caller's progressed signal).

Run: python3 tests/discord-proactive-send.test.py
"""
# ruff: noqa: E402 — imports follow the sys.path inserts below
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import json

import discord_proactive_send as ps
import send_failure_policy
from ag2_sparrow.delivery_core.contract import (
    DeliveryOutcome as CO,
    DeliveryReceipt,
)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


class StubProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def deliver(self, item_id, payload, idempotency_key):
        self.calls.append((item_id, json.loads(payload), idempotency_key))
        out = self.outcomes.pop(0)
        return DeliveryReceipt(out, provider_ref="m" if out is CO.CONFIRMED else None,
                               detail=out.value)


def chunker3(text):
    return [text[i:i + 3] for i in range(0, len(text), 3)]


def main() -> int:
    # 1. All-confirmed: every chunk delivered, ordered keys, right channel.
    p = StubProvider([CO.CONFIRMED] * 3)
    n = ps.deliver_text(p, 123, "abcdefgh", "proactive-1", chunker3)
    check("all chunks confirmed -> count", n == 3)
    check("chunks in order with content",
          [c[1]["content"] for c in p.calls] == ["abc", "def", "gh"])
    check("channel id threaded", all(c[1]["channel_id"] == "123" for c in p.calls))
    check("per-chunk idempotency keys",
          [c[2] for c in p.calls] == ["proactive-1#0", "proactive-1#1", "proactive-1#2"])

    # 2. UNKNOWN raises the policy's capped-transient class — the bridge's
    #    attempt budget sees it; blind unbounded resend is structurally out.
    p = StubProvider([CO.CONFIRMED, CO.OUTCOME_UNKNOWN])
    try:
        ps.deliver_text(p, 1, "abcdef", "i", chunker3)
        check("UNKNOWN raises UnconfirmedDelivery", False)
    except send_failure_policy.UnconfirmedDelivery as e:
        check("UNKNOWN raises UnconfirmedDelivery", True)
        check("UNKNOWN is transient (capped) per policy",
              send_failure_policy.is_transient(e))
    check("UNKNOWN: no chunk sent after the ambiguous one", len(p.calls) == 2)

    # 3. NOT_DELIVERED raises ProviderSendFailed, non-transient, with the
    #    progressed count.
    p = StubProvider([CO.CONFIRMED, CO.NOT_DELIVERED])
    try:
        ps.deliver_text(p, 1, "abcdef", "i", chunker3)
        check("NOT_DELIVERED raises ProviderSendFailed", False)
    except ps.ProviderSendFailed as e:
        check("NOT_DELIVERED raises ProviderSendFailed", True)
        check("refusal is permanent per policy",
              not send_failure_policy.is_transient(e))
        check("sent_chunks carries progress", e.sent_chunks == 1)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: proactive send-leg — ordered confirmed chunks, capped-transient "
          "UNKNOWN, permanent refusal with progress")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
