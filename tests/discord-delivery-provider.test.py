#!/usr/bin/env python3
"""DiscordDeliveryProvider: transport receipts translate to contract receipts,
config errors raise (never ambiguity), and the wiring proof — the REAL
DeliveryCore drains through the REAL DesignAClaimBackend with this provider:
CONFIRMED archives the item; OUTCOME_UNKNOWN (capabilities off) completes
toward the park path, and the claim is not left dangling.

Run: python3 tests/discord-delivery-provider.test.py
"""
# ruff: noqa: E402 — imports follow the sys.path inserts below
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import backend_a
from ag2_sparrow.delivery_core.contract import DeliveryAttempt
from ag2_sparrow.delivery_core.contract import DeliveryOutcome as CO
from ag2_sparrow.delivery_core.core import DeliveryCore
from outbox import DeliveryOutcome as TO, RetrySafety
from outbox_adapter import DeliveryReceipt as TransportReceipt

spec = importlib.util.spec_from_file_location(
    "ddp", REPO / "src" / "channels" / "discord" / "delivery_provider.py")
ddp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ddp)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


class StubClient:
    def __init__(self, outcome=TO.CONFIRMED, rid="m-1"):
        self.calls = []
        self.outcome = outcome
        self.rid = rid

    def send_message(self, channel_id, payload):
        self.calls.append((channel_id, payload))
        return TransportReceipt(self.outcome,
                                receipt_id=self.rid if self.outcome is TO.CONFIRMED else None,
                                safety=RetrySafety.UNSAFE, detail="stub")


def payload(channel="123", content="hello"):
    return json.dumps({"channel_id": channel, "content": content}).encode()


def main() -> int:
    # 1. Receipt translation, all three outcomes.
    p = ddp.DiscordDeliveryProvider(StubClient(TO.CONFIRMED))
    r = p.deliver("i1", payload(), "i1#0")
    check("CONFIRMED maps with provider_ref",
          r.outcome is CO.CONFIRMED and r.provider_ref == "m-1")
    p = ddp.DiscordDeliveryProvider(StubClient(TO.NOT_DELIVERED))
    check("NOT_DELIVERED maps", p.deliver("i1", payload(), "k").outcome is CO.NOT_DELIVERED)
    p = ddp.DiscordDeliveryProvider(StubClient(TO.OUTCOME_UNKNOWN))
    check("OUTCOME_UNKNOWN maps", p.deliver("i1", payload(), "k").outcome is CO.OUTCOME_UNKNOWN)

    # 2. Config errors raise; they never read as delivery ambiguity.
    p = ddp.DiscordDeliveryProvider(StubClient())
    try:
        p.deliver("i1", json.dumps({"content": "no channel"}).encode(), "k")
        check("missing channel_id raises", False)
    except KeyError:
        check("missing channel_id raises", True)
    try:
        p.deliver("i1", b"not json", "k")
        check("non-JSON payload raises", False)
    except Exception as e:
        check("non-JSON payload raises", not isinstance(e, AssertionError))

    # 3. Capabilities: both post-UNKNOWN paths declared off.
    caps = ddp.DiscordDeliveryProvider.capabilities
    check("capabilities off (park on UNKNOWN)",
          caps.reconcile_capable is False and caps.idempotent_send is False)

    # 4. Wiring proof: REAL core + REAL backend + this provider on a tmp root.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = backend_a.DesignAClaimBackend(root)
        client = StubClient(TO.CONFIRMED)
        core = DeliveryCore(backend, ddp.DiscordDeliveryProvider(client))
        backend.publish("item-1", payload("999", "via core"))
        res = core.deliver_one("item-1", payload("999", "via core"))
        check("core drain: CONFIRMED outcome", res.outcome is CO.CONFIRMED)
        check("core drain: provider called once with the channel",
              client.calls == [("999", {"content": "via core"})])
        check("core drain: item reached a terminal state (no dangling claim)",
              core.deliver_one("item-1", payload()).outcome is None)

        backend.publish("item-2", payload("999", "will be unknown"))
        client2 = StubClient(TO.OUTCOME_UNKNOWN)
        core2 = DeliveryCore(backend, ddp.DiscordDeliveryProvider(client2))
        res = core2.deliver_one("item-2", payload("999", "will be unknown"))
        check("core drain: UNKNOWN with caps off stays UNKNOWN (no blind resend)",
              res.outcome is CO.OUTCOME_UNKNOWN and len(client2.calls) == 1)

    # 4. reconcile takes the contract's DeliveryAttempt. Called directly, because
    # isinstance(p, DeliveryProvider) is True for a stale (item_id, key) signature.
    p = ddp.DiscordDeliveryProvider(StubClient(TO.CONFIRMED))
    attempt = DeliveryAttempt("i1", payload(), "i1#0")
    check("reconcile accepts a DeliveryAttempt positionally",
          p.reconcile(attempt) is None)
    check("and by keyword, so the parameter is named `attempt`",
          p.reconcile(attempt=attempt) is None)
    try:
        p.reconcile("i1", "i1#0")
        check("the OLD (item_id, key) call is rejected", False,
              "stale two-arg call still succeeded")
    except TypeError:
        check("the OLD (item_id, key) call is rejected", True)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: DiscordDeliveryProvider — contract receipts, raising config "
          "errors, real core+backend wiring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
