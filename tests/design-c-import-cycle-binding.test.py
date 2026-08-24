#!/usr/bin/env python3
"""The importer's skip checks must bind to the A record that produced C's
representation, not merely to its presence. An A rollback + republish
otherwise leaves C serving the previous cycle's payload or receipt."""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import migration as mig  # noqa: E402
from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend  # noqa: E402
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    DesignCClaimBackend, _safe_key)
from ag2_sparrow.delivery_core.contract import DeliveryOutcome  # noqa: E402

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _rollback(root):
    """Put A's items back and reset the fence, as a re-run would."""
    (root / ".items-migrated").rename(root / ".items")
    mig.write_fence(root, "A")


# --- READY: republished with new content --------------------------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"OLD-PAYLOAD")
    r1 = mig.import_a_state(root)
    check(r1.get("ready") == 1 and r1.get("fenced") is True,
          f"first import publishes the item ({r1})")

    _rollback(root)
    # A's publish is create-if-absent: without dropping the record first,
    # A still holds OLD-PAYLOAD and skipping would be the correct answer.
    for f in (root / ".items").glob("ready-1.*.json"):
        f.unlink()
    a2 = DesignAClaimBackend(root)
    a2.publish("ready-1", b"NEW-PAYLOAD")

    r2 = mig.import_a_state(root)
    c = DesignCClaimBackend(root)
    served = (c._d("ready") / _safe_key("ready-1")).read_bytes()
    check(r2.get("conflicts") == [_safe_key("ready-1")],
          f"the republish is reported as a conflict ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")
    check(served == b"OLD-PAYLOAD",
          "C's existing payload is left intact, not silently clobbered")

# --- DELIVERED: republished to a new destination -------------------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("done-1", b"sent")
    tok = a.claim("done-1", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED,
               provider="p1", destination="OLD-DEST")
    r1 = mig.import_a_state(root)
    check(r1.get("delivered") == 1 and r1.get("fenced") is True,
          f"first import records the terminal ({r1})")

    _rollback(root)
    a2 = DesignAClaimBackend(root)
    a2.publish("done-1", b"sent")
    tok2 = a2.claim("done-1", "w0")
    a2.complete(tok2, DeliveryOutcome.CONFIRMED,
                provider="p1", destination="NEW-DEST")

    r2 = mig.import_a_state(root)
    check(r2.get("conflicts") == [_safe_key("done-1")],
          f"the new receipt is reported as a conflict ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")
    resolved = DesignCClaimBackend(root).terminal_record("done-1")
    check(resolved["receipt"]["destination"] == "OLD-DEST",
          "C's confirmed receipt is not overwritten by the importer")

# --- an unchanged re-run is still idempotent, not a conflict -------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"same")
    a.publish("parked-1", b"held")
    a.park("parked-1", "operator-hold")
    mig.import_a_state(root)
    _rollback(root)
    r2 = mig.import_a_state(root)
    check("conflicts" not in r2,
          f"an identical re-run reports no conflict ({r2})")
    check(r2.get("verified") is True and r2.get("fenced") is True,
          f"an identical re-run still verifies and fences ({r2})")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
