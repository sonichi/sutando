#!/usr/bin/env python3
"""A->C importer (the ruling's migration workstream): every A state maps,
the pass is idempotent, originals survive, and the fence is written LAST."""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import ag2_sparrow.outbox as outbox  # noqa: E402
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    SEP, DesignCClaimBackend, _safe_key)
from ag2_sparrow.delivery_core.migration import (  # noqa: E402
    import_a_state, read_epoch)
from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend  # noqa: E402
from ag2_sparrow.delivery_core.contract import DeliveryOutcome  # noqa: E402

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    # READY item
    a.publish("ready-1", b"hello")
    # DELIVERED item with a persisted receipt
    a.publish("done-1", b"sent")
    tok = a.claim("done-1", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED,
               provider="DiscordDeliveryProvider", destination="chan-9")
    # PARKED item
    a.publish("parked-1", b"held")
    a.park("parked-1", "operator-hold")
    # attempts on the ready item
    a.publish("tried-1", b"try")
    t2 = a.claim("tried-1", "w0")
    a.complete(t2, DeliveryOutcome.NOT_DELIVERED)   # attempts=1, back to ready

    rep = import_a_state(root)
    check(rep["verified"] and rep["fenced"],
          f"import verifies and fences ({rep})")
    check(rep["ready"] == 2 and rep["delivered"] == 1 and rep["parked"] == 1,
          f"per-state counts match the fixture ({rep})")
    check(read_epoch(root) == "C", "epoch fence names C")
    check(not (root / ".items").exists() and (root / ".items-migrated").is_dir(),
          "originals preserved under .items-migrated (rollback = rename back)")

    c = DesignCClaimBackend(root)                   # activated by the import
    check((c._d("ready") / _safe_key("ready-1")).exists(),
          "READY item is claimable in C")
    rec = c.terminal_record("done-1")
    check(rec is not None and rec["receipt"]["destination"] == "chan-9"
          and rec.get("imported") is True,
          f"DELIVERED item's receipt survived the import ({rec})")
    check(any(e.name.startswith(f"{_safe_key('parked-1')}{SEP}operator-hold")
              for e in c._d("undelivered").iterdir()),
          "PARKED item kept its reason in C undelivered/")
    check(c.attempts("tried-1") == 1, "attempt count carried over")
    t = c.claim("ready-1", "w9")
    check(t is not None and c.complete(t, DeliveryOutcome.CONFIRMED,
                                       provider="P", destination="D"),
          "imported item completes through the full C lifecycle")

    # Idempotency: a second run on the migrated root is a clean no-op.
    rep2 = import_a_state(root)
    check(rep2["verified"] and sum(rep2[k] for k in
          ("ready", "parked", "delivered", "unknown", "skipped")) == 0,
          f"re-run after fence is a no-op ({rep2})")

with tempfile.TemporaryDirectory() as td:
    # Partial-crash resume: first run interrupted -> re-run completes.
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    for i in range(4):
        a.publish(f"item-{i}", f"p{i}".encode())
    rep1 = import_a_state(root)
    check(rep1["ready"] == 4 and rep1["fenced"], "clean full pass")
    # simulate a HALF-imported root: rebuild A items, pre-seed C with 2
    root2 = Path(td) / "root2"
    a2 = DesignAClaimBackend(root2)
    for i in range(4):
        a2.publish(f"item-{i}", f"p{i}".encode())
    c2 = DesignCClaimBackend(root2, activate=True)
    for i in range(2):
        c2.publish(f"item-{i}", f"p{i}".encode())
    rep3 = import_a_state(root2)
    check(rep3["ready"] == 2 and rep3["skipped"] == 2 and rep3["fenced"],
          f"resume imports only the missing half ({rep3})")

with tempfile.TemporaryDirectory() as td:
    # A claimed-but-unresolved item lands in reconcile territory.
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("limbo-1", b"x")
    a.claim("limbo-1", "w-dead")                    # never completed
    rep = import_a_state(root)
    check(rep["unknown"] == 1 and rep["fenced"],
          f"claimed non-terminal maps to import-outcome-unknown ({rep})")
    c = DesignCClaimBackend(root)
    check(any("import-outcome-unknown" in e.name
              for e in c._d("undelivered").iterdir()),
          "and the marker names the reconcile reason")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
