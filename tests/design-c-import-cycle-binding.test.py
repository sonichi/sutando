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
    DesignCClaimBackend, SEP, _safe_key)
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

# --- CLAIMED + UNRESOLVED: the import-outcome-unknown marker ---------------
# keweichen's P2, driven only through A operations.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("stuck-1", b"OLD-PAYLOAD")
    a.claim("stuck-1", "w0")                 # claimed, never completed
    r1 = mig.import_a_state(root)
    check(r1.get("unknown") == 1 and r1.get("fenced") is True,
          f"first import stages the outcome-unknown marker ({r1})")

    _rollback(root)
    for f in (root / ".items").glob("stuck-1.*.json"):
        f.unlink()                           # A refuses re-publish over a live claim
    a2 = DesignAClaimBackend(root)
    a2.publish("stuck-1", b"NEW-PAYLOAD")
    a2.claim("stuck-1", "w0")

    r2 = mig.import_a_state(root)
    c = DesignCClaimBackend(root)
    marker = (c._d("undelivered")
              / f"{_safe_key('stuck-1')}{SEP}import-outcome-unknown{SEP}import")
    check(r2.get("conflicts") == [_safe_key("stuck-1")],
          f"the republished claim is reported as a conflict ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")
    check(marker.read_bytes() == b"OLD-PAYLOAD",
          "C is not fenced onto the new payload behind the old marker")

# --- DELIVERED WITHOUT A RECEIPT: the outcome-unknown marker ---------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("bare-1", b"OLD-PAYLOAD")
    tok = a.claim("bare-1", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED)   # bare sentinel: no receipt
    r1 = mig.import_a_state(root)
    check(r1.get("unknown") == 1,
          f"a bare sentinel imports as outcome-unknown, not delivered ({r1})")

    _rollback(root)
    a2 = DesignAClaimBackend(root)
    a2.publish("bare-1", b"NEW-PAYLOAD")         # DELIVERED allows a fresh cycle
    tok2 = a2.claim("bare-1", "w0")
    a2.complete(tok2, DeliveryOutcome.CONFIRMED)

    r2 = mig.import_a_state(root)
    check(r2.get("conflicts") == [_safe_key("bare-1")],
          f"the new cycle's body is reported as a conflict ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")

# --- QUARANTINE: a record whose name does not bind to its body ------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("anchor-1", b"anchor")          # so .items exists
    bogus = root / ".items" / "not-the-key.json"
    bogus.write_text('{"item_id": "anchor-1", "payload": "OLD"}')
    r1 = mig.import_a_state(root)
    check(r1.get("unknown") == 1,
          f"the mismatched record is quarantined, not imported ({r1})")

    _rollback(root)
    bogus = root / ".items" / "not-the-key.json"
    bogus.write_text('{"item_id": "anchor-1", "payload": "NEW"}')
    r2 = mig.import_a_state(root)
    check(r2.get("conflicts") == [_safe_key("not-the-key")],
          f"a changed quarantined body is a conflict, not a skip ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")

# --- THE RULE MUST STILL SAY YES ------------------------------------------
# Without this, a predicate that conflicts on everything passes all six above.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("stuck-2", b"same")
    a.claim("stuck-2", "w0")
    a.publish("bare-2", b"same")
    tok = a.claim("bare-2", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED)
    (root / ".items" / "not-the-key.json").write_text(
        '{"item_id": "stuck-2", "payload": "same"}')
    mig.import_a_state(root)
    _rollback(root)
    r2 = mig.import_a_state(root)
    check("conflicts" not in r2,
          f"an identical re-run of all three branches is not a conflict ({r2})")
    check(r2.get("verified") is True and r2.get("fenced") is True,
          f"an identical re-run still verifies and fences ({r2})")


print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
