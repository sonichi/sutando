#!/usr/bin/env python3
"""A delivered item must record WHERE it went, durably.

The provider/destination line is logged, and logs rotate; after that the
receipt is the only thing that can answer "delivered to where". Absent values
must stay absent rather than becoming a destination nobody observed.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

spec = importlib.util.spec_from_file_location("outbox", REPO / "src" / "outbox.py")
outbox = importlib.util.module_from_spec(spec)
# Register before exec: @dataclass resolves annotations via sys.modules[__module__].
sys.modules["outbox"] = outbox
spec.loader.exec_module(outbox)

failures = []


def check(cond, label):
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        outbox.record_delivered(root, "task-A", provider="AG2SpaceResultProvider",
                                destination="!room:ag2.space")
        a = outbox._read_item(root, "task-A")
        check(a.get("status") == "DELIVERED", "records the DELIVERED status")
        check(a.get("provider") == "AG2SpaceResultProvider", "persists the provider")
        check(a.get("destination") == "!room:ag2.space", "persists the destination")

        # Back-compat: a caller with nothing to assert must not invent one.
        outbox.record_delivered(root, "task-B")
        b = outbox._read_item(root, "task-B")
        check(b.get("status") == "DELIVERED", "still marks delivered without a destination")
        check("destination" not in b, "absent destination is OMITTED, not stored as null")
        check("provider" not in b, "absent provider is OMITTED, not stored as null")

        # An item written before this existed reads back as None, not as a guess.
        outbox._write_item(root, "task-legacy", {"item_id": "task-legacy",
                                                 "status": "DELIVERED", "attempts": 1})
        legacy = outbox._read_item(root, "task-legacy")
        check(legacy.get("destination") is None,
              "a pre-existing item reports destination None rather than a fabricated value")

        # The receipt type must be able to carry it, or the provider cannot report it.
        from ag2_sparrow.delivery_core.contract import DeliveryReceipt, DeliveryOutcome
        r = DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED, destination="!x:y")
        check(r.destination == "!x:y", "DeliveryReceipt carries a destination")
        check(DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED).destination is None,
              "destination defaults to None so existing providers keep working")

    print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
