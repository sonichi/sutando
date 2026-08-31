#!/usr/bin/env python3
"""The importer's skip checks must bind to the A record that produced C's
representation, not merely to its presence. An A rollback + republish
otherwise leaves C serving the previous cycle's payload or receipt."""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import migration as mig  # noqa: E402
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    DesignCClaimBackend, SEP, _safe_key)
from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend  # noqa: E402
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


def _dead_claim(root, item_id, worker="w0"):
    """Claim in C from a child that exits without completing: leaves a dead
    producer-valid token whose BYTES are the claimed cycle's payload."""
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(REPO / 'packages' / 'ag2-sparrow')!r})\n"
        "from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend\n"
        f"tok = DesignCClaimBackend(Path({str(root)!r}),"
        f" activate=True).claim({item_id!r}, {worker!r})\n"
        "sys.exit(0 if tok else 3)\n"
    )
    return subprocess.run([sys.executable, "-c", code]).returncode == 0


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

# --- CLAIMED IN C, CLAIMANT DIED: a token is content, not grammar ---------
# Grammar-only membership verified a NEW A body behind a dead OLD token.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"OLD-PAYLOAD")
    mig.import_a_state(root)
    check(_dead_claim(root, "ready-1"),
          "a C claimant takes the token and dies holding it")

    _rollback(root)
    for f in (root / ".items").glob("ready-1.*.json"):
        f.unlink()
    a2 = DesignAClaimBackend(root)
    a2.publish("ready-1", b"NEW-PAYLOAD")

    r2 = mig.import_a_state(root)
    c = DesignCClaimBackend(root)
    toks = c._tokens(_safe_key("ready-1"))
    check(r2.get("conflicts") == [_safe_key("ready-1")],
          f"the dead token's stale body is a conflict, not a skip ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")
    check(bool(toks) and toks[0].read_bytes() == b"OLD-PAYLOAD",
          "the token is left intact for recover(), not clobbered")

# --- ...and the identical-payload dead token still says yes ---------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"SAME-PAYLOAD")
    mig.import_a_state(root)
    check(_dead_claim(root, "ready-1"),
          "a C claimant takes the token and dies holding it")

    _rollback(root)
    r2 = mig.import_a_state(root)
    check("conflicts" not in r2 and r2.get("skipped", 0) >= 1,
          f"an identical dead token is ownership, not a conflict ({r2})")
    check(r2.get("verified") is True and r2.get("fenced") is True,
          f"the identical re-run verifies and fences ({r2})")

# --- SYMLINKED EVIDENCE: bytes behind a symlink are not writer state ------
# External-file symlinks passed _same_bytes; the store held nothing.
import os

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    external = Path(td) / "external-payload"
    external.write_bytes(b"SAME-PAYLOAD")
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"SAME-PAYLOAD")
    mig.import_a_state(root)
    check(_dead_claim(root, "ready-1"),
          "a C claimant takes the token and dies holding it")
    # swap the token for a symlink to the external file (same bytes)
    tok = next((root / "inflight").iterdir())
    tok.unlink()
    os.symlink(external, tok)
    _rollback(root)
    r2 = mig.import_a_state(root)
    check(r2.get("conflicts") == [_safe_key("ready-1")],
          f"a symlinked token is a conflict even with matching bytes ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    external = Path(td) / "external-payload"
    external.write_bytes(b"SAME-PAYLOAD")
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"SAME-PAYLOAD")
    mig.import_a_state(root)
    c = DesignCClaimBackend(root)
    rp = c._d("ready") / _safe_key("ready-1")
    rp.unlink()
    os.symlink(external, rp)
    _rollback(root)
    r2 = mig.import_a_state(root)
    check(r2.get("conflicts") == [_safe_key("ready-1")],
          f"a symlinked ready entry is a conflict even with matching bytes ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")

# --- BARE SENTINEL vs A RECEIPT-LESS PRIOR TERMINAL -----------------------
# (None, None) matched, laundering a bare sentinel through an old terminal.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    c = DesignCClaimBackend(root, activate=True)
    c.publish("bare-3", b"OLD")
    tok = c.claim("bare-3", "w0")
    c.complete(tok, DeliveryOutcome.CONFIRMED)      # prior cycle, NO receipt
    a = DesignAClaimBackend(root)
    a.publish("bare-3", b"NEW")
    t2 = a.claim("bare-3", "w0")
    a.complete(t2, DeliveryOutcome.CONFIRMED)       # bare A sentinel
    r = mig.import_a_state(root)
    check(r.get("conflicts") == [_safe_key("bare-3")],
          f"a bare sentinel never reuses a terminal — collision conflicts ({r})")
    check(r.get("fenced") is not True,
          "the fence is withheld while a conflict stands")

# ...and receipt-BEARING reuse still says yes (identical receipt re-import)
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("done-2", b"sent")
    tok = a.claim("done-2", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED,
               provider="p1", destination="SAME-DEST")
    mig.import_a_state(root)
    _rollback(root)
    r2 = mig.import_a_state(root)
    check("conflicts" not in r2 and r2.get("skipped", 0) >= 1,
          f"an identical receipt-bearing record reuses its terminal ({r2})")
    check(r2.get("verified") is True and r2.get("fenced") is True,
          f"the receipt-bearing re-run verifies and fences ({r2})")

# --- SAME ROUTE, NEW CYCLE: a terminal binds to the RECORD, not the route --
# Route-only reuse discarded a fresh A cycle's payload/attempts/history.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("done-3", b"CYCLE-ONE")
    tok = a.claim("done-3", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED, provider="p1", destination="DST")
    mig.import_a_state(root)
    _rollback(root)
    for f in (root / ".items").glob("done-3.*.json"):
        f.unlink()
    a2 = DesignAClaimBackend(root)
    a2.publish("done-3", b"CYCLE-TWO")            # new payload, same route
    t1 = a2.claim("done-3", "w0")
    a2.complete(t1, DeliveryOutcome.NOT_DELIVERED)           # one failed attempt
    t2 = a2.claim("done-3", "w0")
    a2.complete(t2, DeliveryOutcome.CONFIRMED, provider="p1", destination="DST")
    r2 = mig.import_a_state(root)
    check(r2.get("conflicts") == [_safe_key("done-3")],
          f"same-route new-cycle record is a conflict, not a skip ({r2})")
    check(r2.get("fenced") is not True,
          "the fence is withheld while a conflict stands")

# --- UNREADABLE SOURCE: enumeration denial is not an empty migration ------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("perm-1", b"payload")
    (root / ".items").rename(root / ".items-migrated")     # resume window
    os.chmod(root / ".items-migrated", 0o000)
    try:
        r = mig.import_a_state(root)
    finally:
        os.chmod(root / ".items-migrated", 0o700)
    check("unmigratable" in r and r.get("fenced") is not True,
          f"denied enumeration fails closed before fencing ({r})")
    check(mig.read_epoch(root) != "C", "denied-source root never reaches epoch C")

# --- MIXED TOKENS: one valid sibling does not launder a malformed one -----
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"SAME-PAYLOAD")
    mig.import_a_state(root)
    check(_dead_claim(root, "ready-1"),
          "a C claimant takes the token and dies holding it")
    (root / "inflight" / f"{_safe_key('ready-1')}{SEP}junk").write_bytes(b"SAME-PAYLOAD")
    _rollback(root)
    r2 = mig.import_a_state(root)
    check(r2.get("verified") is not True and r2.get("fenced") is not True,
          f"a malformed sibling keeps the root unfenced ({r2})")
    check(any("junk" in t for t in r2.get("malformed_tokens", [])),
          f"the malformed sibling is REPORTED, not skipped forever ({r2})")

# --- SYMLINKED ATTEMPTS: budget evidence must be writer state too ---------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    external = Path(td) / "external-attempts"
    external.write_text("2")     # SAME value as the record: only the
    # regular-file predicate, not a value mismatch, can force the replace
    a = DesignAClaimBackend(root)
    a.publish("ready-1", b"pay")
    c = DesignCClaimBackend(root, activate=True)
    ap = c._attempts_path(_safe_key("ready-1"))
    ap.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(external, ap)
    # record carries attempts=2: the symlink must be REPLACED, not trusted
    import json as _tj
    for f in (root / ".items").glob("ready-1.*.json"):
        rec = _tj.loads(f.read_text())
        rec["attempts"] = 2
        f.write_text(_tj.dumps(rec))
    mig.import_a_state(root)
    import stat as _stat
    check(_stat.S_ISREG(os.lstat(ap).st_mode),
          "the symlinked attempts entry is replaced by a regular file")
    check(ap.read_text().strip() == "2",
          f"the budget comes from A's record, not the symlink target ({ap.read_text()!r})")
    external.unlink()
    check(ap.read_text().strip() == "2",
          "removing the external target no longer changes C's count")

# --- MALFORMED COUNTER: the shared validator blocks, never measures zero --
from ag2_sparrow.delivery_core.migration import read_fallback_counter
with tempfile.TemporaryDirectory() as td:
    cpath = Path(td) / "a-fallback-hits.json"
    for bad in ('{"count": -5}', '{"count": true}', '{"count": "7"}',
                '{"count": 10000000000000}', '[]', 'garbage'):
        cpath.write_text(bad)
        check(read_fallback_counter(cpath) is None,
              f"validator rejects {bad!r}")
    cpath.write_text('{"count": 3}')
    check(read_fallback_counter(cpath) == 3, "control: a valid count reads")
    # miss path leaves a malformed file untouched (probe flags it; the
    # hit path would repair and hide that garbage happened)
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("cx", b"p")
    mig.import_a_state(root)
    bad_counter = root / "a-fallback-hits.json"
    bad_counter.write_text('{"count": -5}')
    miss = mig.dual_read(root, "no-such-id")
    check(miss is None, "control: dual_read miss returns None")
    check(bad_counter.read_text() == '{"count": -5}',
          f"the miss path does not silently repair garbage ({bad_counter.read_text()!r})")
    check(read_fallback_counter(bad_counter) is None,
          "...and the shared validator flags it, blocking the gate")

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
