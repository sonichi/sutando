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
    # The pseudo-incarnation property, measured on a REAL A DELIVERED record
    # (review ask): 2 SEP-parts, never TOKEN_PARTS — cannot match any claim.
    from ag2_sparrow.delivery_core.backend_c import (
        SEP as _SEP, TOKEN_PARTS as _TP, DesignCClaimBackend as _C)
    _rec = _C(root).terminal_record("done-1")
    _parts = _rec["incarnation"].split(_SEP)
    check(len(_parts) == 2 and len(_parts) != _TP,
          f"imported incarnation has {len(_parts)} parts (2, not TOKEN_PARTS={_TP})")
    check(_rec["imported"] is True and _rec["receipt"]["destination"] == "chan-9",
          "the real A receipt survived the import intact")
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


with tempfile.TemporaryDirectory() as td:
    # CRASH WINDOW (Codex control): rename done, fence never written.
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("w-1", b"x")
    a.publish("w-2", b"y")
    import ag2_sparrow.delivery_core.migration as mig
    real_fence = mig.write_fence
    def crashing_fence(r, e):
        raise RuntimeError("simulated crash inside write_fence")
    mig.write_fence = crashing_fence
    try:
        try:
            import_a_state(root)
        except RuntimeError:
            pass
    finally:
        mig.write_fence = real_fence
    check(not (root / ".items").exists() and (root / ".items-migrated").is_dir(),
          "crash window reproduced: renamed but unfenced")
    check(read_epoch(root) == "A", "epoch still A after the crash")
    rep = import_a_state(root)                     # the rerun must RECOVER
    check(rep["verified"] and rep["fenced"],
          f"rerun re-verifies the preserved originals and finishes the fence ({rep})")
    check(read_epoch(root) == "C", "epoch reaches C — migration not stranded")
    c = DesignCClaimBackend(root)
    check((c._d("ready") / _safe_key("w-1")).exists(),
          "and the imported items are intact")

with tempfile.TemporaryDirectory() as td:
    # Ambiguous .items shapes fail closed; a clean root fences intentionally.
    root = Path(td) / "file-shape"
    root.mkdir()
    (root / ".items").write_text("not a dir")
    rep = import_a_state(root)
    check(not rep["verified"] and not rep["fenced"] and "unmigratable" in rep,
          f"a FILE at .items fails closed ({rep})")
    root2 = Path(td) / "danglink"
    root2.mkdir()
    (root2 / ".items").symlink_to(Path(td) / "gone")
    rep2 = import_a_state(root2)
    check(not rep2["verified"] and not rep2["fenced"],
          "a dangling .items symlink fails closed")
    root3 = Path(td) / "clean"
    root3.mkdir()
    rep3 = import_a_state(root3)
    check(rep3["verified"] and rep3["fenced"] and read_epoch(root3) == "C",
          f"a genuinely clean root completes activation and fences ({rep3})")
    root4 = Path(td) / "mig-symlink"
    root4.mkdir()
    ext = Path(td) / "external-empty"
    ext.mkdir()
    (root4 / ".items-migrated").symlink_to(ext)
    rep4 = import_a_state(root4)
    check(not rep4["verified"] and not rep4["fenced"] and "unmigratable" in rep4,
          f"a SYMLINKED .items-migrated fails closed, unfenced ({rep4})")
    root5 = Path(td) / "mig-file"
    root5.mkdir()
    (root5 / ".items-migrated").write_text("x")
    rep5 = import_a_state(root5)
    check(not rep5["verified"] and not rep5["fenced"],
          "a FILE at .items-migrated fails closed")
    root6 = Path(td) / "mig-danglink"
    root6.mkdir()
    (root6 / ".items-migrated").symlink_to(Path(td) / "nope")
    rep6 = import_a_state(root6)
    check(not rep6["verified"] and not rep6["fenced"],
          "a dangling .items-migrated symlink fails closed")

with tempfile.TemporaryDirectory() as td:
    # Malformed A record: ONE stable marker across repeated imports.
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("good-1", b"x")
    (root / ".items" / "broken.json").write_text("{not json")
    rep1 = import_a_state(root)
    # the malformed record blocks nothing; re-import the migrated root twice
    c = DesignCClaimBackend(root)
    markers1 = [e.name for e in c._d("undelivered").iterdir() if "import-unreadable" in e.name]
    check(len(markers1) == 1, f"one marker for the malformed record ({markers1})")
    import_a_state(root)                            # no-op post-fence
    markers2 = [e.name for e in c._d("undelivered").iterdir() if "import-unreadable" in e.name]
    check(markers2 == markers1, "repeated import creates no duplicate markers")

# ── dual_read: the migration-window C-miss fallback ────────────────────
from ag2_sparrow.delivery_core.migration import (  # noqa: E402
    FALLBACK_COUNTER, dual_read, resolve_delivery)

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "dr"
    outbox._write_item(root, "hist-1", {"item_id": "hist-1", "status": "DELIVERED",
                                 "provider": "P", "destination": "D",
                                 "attempts": 2})
    rep = import_a_state(root)
    check(rep["fenced"], "dual_read fixture: import fenced")
    before = {d: sorted(x.name for x in (root / d).iterdir())
              for d in ("ready", "inflight", "undelivered")}

    rec = dual_read(root, "hist-1")
    check(rec is not None and rec.get("destination") == "D",
          "dual_read returns the preserved A record on a hit")
    ctr = json.loads((root / FALLBACK_COUNTER).read_text())
    check(ctr["count"] == 1 and ctr["last_item"] == "hist-1",
          "a hit writes the fallback counter (deletion release-gate metric)")
    dual_read(root, "hist-1")
    check(json.loads((root / FALLBACK_COUNTER).read_text())["count"] == 2,
          "counter is cumulative across hits")

    check(dual_read(root, "no-such-id") is None,
          "a miss returns None")
    check(json.loads((root / FALLBACK_COUNTER).read_text())["count"] == 2,
          "and a miss does not bump the counter")

    # a counter that parses to a non-dict (torn/truncated write shape) must
    # not crash the read path; the next hit rebuilds it from zero
    (root / FALLBACK_COUNTER).write_text("null")
    rec = dual_read(root, "hist-1")
    check(rec is not None
          and json.loads((root / FALLBACK_COUNTER).read_text())["count"] == 1,
          "non-dict counter is tolerated: hit still serves and recounts from 0")

    after = {d: sorted(x.name for x in (root / d).iterdir())
             for d in ("ready", "inflight", "undelivered")}
    check(after == before,
          "dual_read never writes to C — no resurrection into any state dir")
    a_name = f"{outbox._safe_key('hist-1')}.json"
    check((root / ".items-migrated" / a_name).exists(),
          "and the preserved A record is untouched")

    # a record whose body names a different item_id is not served, even
    # though the file EXISTS at the looked-up name (discriminates from a miss)
    (root / ".items-migrated" / f"{outbox._safe_key('alias')}.json").write_text(
        json.dumps({"item_id": "somebody-else"}))
    check(dual_read(root, "alias") is None,
          "body/item_id mismatch is refused (no serving mislabeled records)")

with tempfile.TemporaryDirectory() as td:
    # ── resolve_delivery: the consumer-wiring surface over C + dual_read ──
    root = Path(td) / "resolver"
    outbox._write_item(root, "old-1", {"item_id": "old-1", "status": "DELIVERED",
                                       "provider": "P", "destination": "D9",
                                       "attempts": 1})
    import_a_state(root)
    r = resolve_delivery(root, "old-1")
    check(r["source"] == "c" and r["delivered"]
          and r["receipt"] == {"provider": "P", "destination": "D9"},
          "resolve_delivery answers an imported id from C (source=c, receipt)")
    ctr_missing = not (root / FALLBACK_COUNTER).exists()
    check(ctr_missing, "a C answer consults no fallback (no counter yet)")

    # an id the importer never covered, present only in the preserved A copy
    ghost = {"item_id": "ghost-7", "status": "DELIVERED",
             "provider": "P", "destination": "G"}
    (root / ".items-migrated" / f"{outbox._safe_key('ghost-7')}.json").write_text(
        json.dumps(ghost))
    r = resolve_delivery(root, "ghost-7")
    check(r["source"] == "a-fallback" and r["delivered"]
          and r["receipt"]["destination"] == "G",
          "C-miss on a fenced root falls through to the preserved A record")
    check(json.loads((root / FALLBACK_COUNTER).read_text())["count"] == 1,
          "and the fallback answer is COUNTED (release-gate metric)")

    r = resolve_delivery(root, "never-was")
    check(r["source"] is None and not r["delivered"],
          "unknown id resolves to source=None, not an invented answer")
    check(json.loads((root / FALLBACK_COUNTER).read_text())["count"] == 1,
          "and a total miss does not bump the counter")

    # a non-DELIVERED preserved record answers with NO receipt
    (root / ".items-migrated" / f"{outbox._safe_key('parked-a')}.json").write_text(
        json.dumps({"item_id": "parked-a", "status": "PARKED"}))
    r = resolve_delivery(root, "parked-a")
    check(r["source"] == "a-fallback" and not r["delivered"]
          and r["receipt"] is None,
          "a preserved non-DELIVERED record is not laundered into a receipt")

with tempfile.TemporaryDirectory() as td:
    # the migration-window-ONLY rule: an epoch-A root gets no fallback even
    # if a .items-migrated dir exists — its live A store owns the answer
    root = Path(td) / "still-a"
    (root / ".items-migrated").mkdir(parents=True)
    (root / ".items-migrated" / f"{outbox._safe_key('x')}.json").write_text(
        json.dumps({"item_id": "x", "status": "DELIVERED"}))
    r = resolve_delivery(root, "x")
    check(r["source"] is None,
          "an un-fenced (epoch A) root never falls back — A tooling owns it")

with tempfile.TemporaryDirectory() as td:
    # extraction pin: the backend method and the module reader agree
    from ag2_sparrow.delivery_core.backend_c import (
        DesignCClaimBackend, read_terminal_records)
    from ag2_sparrow.delivery_core.contract import DeliveryOutcome
    root = Path(td) / "pin"
    b = DesignCClaimBackend(root, activate=True)
    b.publish("p-1", b"x")
    b.complete(b.claim("p-1", "w0"), DeliveryOutcome.CONFIRMED,
               provider="P", destination="D")
    check(b.terminal_records("p-1") == read_terminal_records(root, "p-1")
          and len(read_terminal_records(root, "p-1")) == 1,
          "terminal_records() delegates to the module-level reader (one impl)")

with tempfile.TemporaryDirectory() as td:
    # First consult (even a miss) initializes the counter at 0, so an absent
    # file can only mean "dual_read never ran here" — the gate warns on that.
    root = Path(td) / "fresh"
    outbox._write_item(root, "only", {"item_id": "only", "status": "DELIVERED"})
    import_a_state(root)
    check(not (root / FALLBACK_COUNTER).exists(),
          "import alone writes no counter (dual_read owns the marker)")
    check(dual_read(root, "never-existed") is None, "first consult misses")
    ctr0 = json.loads((root / FALLBACK_COUNTER).read_text())
    check(ctr0["count"] == 0 and "initialized_ts" in ctr0,
          "and initializes the counter at a MEASURED zero (liveness marker)")

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "nofall"
    root.mkdir(parents=True)
    check(dual_read(root, "x") is None,
          "no .items-migrated dir: dual_read is None, no counter write")
    check(not (root / FALLBACK_COUNTER).exists(),
          "and no counter file appears")
    real = Path(td) / "elsewhere"
    real.mkdir()
    (root / ".items-migrated").symlink_to(real)
    check(dual_read(root, "x") is None,
          "a SYMLINKED .items-migrated is refused (same fail-closed rule as import)")

# ── REVIEW BLOCKER CONTROLS (child round: three production paths) ──────────
with tempfile.TemporaryDirectory() as td:
    # 1. a LIVE directory symlink at .items is refused — never followed/fenced
    root = Path(td) / "symroot"
    root.mkdir(parents=True)
    external = Path(td) / "elsewhere"
    external.mkdir()
    (external / "x.json").write_text(json.dumps(
        {"item_id": "x", "status": "DELIVERED"}))
    (root / ".items").symlink_to(external)
    rep = import_a_state(root)
    check(not rep["verified"] and not rep["fenced"]
          and "symlink" in rep.get("unmigratable", ""),
          f"live .items symlink refused, nothing fenced ({rep.get('unmigratable')})")
    check((external / "x.json").read_text() == json.dumps(
              {"item_id": "x", "status": "DELIVERED"})
          and not (root / "protocol-epoch").exists(),
          "external record unchanged AND no protocol-epoch fence written")

with tempfile.TemporaryDirectory() as td:
    # 2. a receipt-less DELIVERED sentinel imports as OUTCOME_UNKNOWN, per the
    #    normative classifier — never a confirmed terminal with a None receipt
    root = Path(td) / "bare"
    outbox._write_item(root, "bare-1", {"item_id": "bare-1",
                                        "status": "DELIVERED"})
    rep = import_a_state(root)
    check(rep["delivered"] == 0 and rep["unknown"] == 1,
          f"bare DELIVERED sentinel counted unknown, not delivered ({rep})")
    from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend as _C2
    check(_C2(root).terminal_record("bare-1") is None,
          "no confirmed terminal record exists for the bare sentinel")
    und = list((root / "undelivered").glob("*outcome-unknown*import*"))
    check(len(und) == 1, "the bare sentinel is parked as outcome-unknown")
    r = resolve_delivery(root, "bare-1")
    check(not r["delivered"] and r["receipt"] is None,
          "resolve_delivery never reports delivered with a None receipt")

with tempfile.TemporaryDirectory() as td:
    # 3. concurrent dual_read: no lost increments, no tmp-name collisions
    import threading as _th
    root = Path(td) / "conc"
    outbox._write_item(root, "hist-c", {"item_id": "hist-c",
                                        "status": "DELIVERED",
                                        "provider": "P", "destination": "D"})
    import_a_state(root)
    ghost = {"item_id": "g", "status": "DELIVERED",
             "provider": "P", "destination": "D"}
    (root / ".items-migrated" / f"{outbox._safe_key('g')}.json").write_text(
        json.dumps(ghost))
    errs, N = [], 64
    def _hit():
        try:
            assert dual_read(root, "g")["item_id"] == "g"
        except Exception as e:      # noqa: BLE001 — the count IS the assertion
            errs.append(repr(e))
    threads = [_th.Thread(target=_hit) for _ in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()
    durable = json.loads((root / FALLBACK_COUNTER).read_text())["count"]
    check(not errs, f"64 concurrent dual_read calls raise nothing ({errs[:2]})")
    check(durable == N, f"durable count {durable} == {N} (no lost increments)")

    # malformed shared-counter shapes (reviewer P1): dual_read never raises,
    # the count fails closed and repairs to a valid int on the next hit
    for label, payload in (("null", '{"count": null}'), ("bool", '{"count": true}'),
                           ("list", '{"count": [1]}'), ("negative", '{"count": -5}'),
                           ("huge", '{"count": 99999999999999999999}'),
                           ("nonfinite", '{"count": NaN}'), ("junk", 'not json')):
        (root / FALLBACK_COUNTER).write_text(payload)
        try:
            r = dual_read(root, "g")
            raised = None
        except Exception as e:                     # noqa: BLE001
            r, raised = None, repr(e)
        check(raised is None and r is not None and r["item_id"] == "g",
              f"malformed counter ({label}): dual_read serves, never raises")
        after = json.loads((root / FALLBACK_COUNTER).read_text())["count"]
        check(isinstance(after, int) and not isinstance(after, bool) and after >= 1,
              f"malformed counter ({label}): repaired to valid int ({after})")


# ── reviewer r2 permanent controls (kewei) ────────────────────────────────────

# 1) epoch gate: B and garbage fail closed; already-C is an explicit no-op
with tempfile.TemporaryDirectory() as td:
    for label, ep in (("B", "B"), ("garbage", "garbage")):
        root = Path(td) / f"r-{label}"
        a = DesignAClaimBackend(root)
        a.publish("x-1", b"body")
        (root / "protocol-epoch").write_text(ep)
        rep = import_a_state(root)
        check("unmigratable" in rep and not rep["fenced"] and not rep["verified"],
              f"epoch {label!r}: import refuses, nothing fenced ({rep})")
        check(read_epoch(root) == ep, f"epoch {label!r} is left untouched")
    root = Path(td) / "r-c"
    root.mkdir(); (root / "protocol-epoch").write_text("C")
    rep = import_a_state(root)
    check(rep.get("noop") and rep["verified"] and rep["fenced"],
          "already-C root: explicit no-op, no state touched")

# 2) malformed token must FAIL verification, never satisfy membership
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-tok"
    a = DesignAClaimBackend(root)
    a.publish("real-1", b"payload")
    from ag2_sparrow.delivery_core.backend_c import INFLIGHT
    from ag2_sparrow.delivery_core.backend_c import _safe_key as _ck
    k = _ck("real-1")
    infl = root / INFLIGHT
    infl.mkdir(parents=True, exist_ok=True)
    (infl / f"{k}~junk").write_text("")      # 2 parts, not TOKEN_PARTS
    rep = import_a_state(root)
    check(not rep["fenced"],
          f"malformed token: fence is NOT written ({rep})")
    check("malformed_tokens" in rep or "missing" in rep,
          "malformed token: named in the report, not silently absorbed")

# 3) per-record symlink is quarantined, never followed
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-sym"
    a = DesignAClaimBackend(root)
    a.publish("good-1", b"fine")
    outside = Path(td) / "outside.json"
    outside.write_text('{"item_id": "evil-1", "status": "QUEUED", "payload": "x"}')
    (root / ".items" / "evil.json").symlink_to(outside)
    rep = import_a_state(root)
    check(rep["unknown"] >= 1, f"symlinked record is quarantined ({rep})")

# 3b) filename<->item_id binding: a renamed record does not import as-is
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-bind"
    a = DesignAClaimBackend(root)
    a.publish("orig-1", b"fine")
    items = root / ".items"
    recs = sorted(items.glob("*.json"))
    recs[0].rename(items / "someothername.json")
    rep = import_a_state(root)
    check(rep["unknown"] >= 1 and not any(
        (root / "ready").glob("*")) if (root / "ready").exists() else rep["unknown"] >= 1,
        f"name-mismatched record is quarantined, not imported ({rep})")

# 4) contract-valid A values migrate: empty-string id + slash reason
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-legal"
    a = DesignAClaimBackend(root)
    a.publish("", b"empty-id body")
    a.publish("held-1", b"held")
    a.park("held-1", "operator/hold")
    rep = import_a_state(root)
    check(rep["verified"] and rep["fenced"],
          f"empty id + slash reason both import and verify ({rep})")
    check(rep["ready"] >= 1, "empty-string item id landed in ready")
    check(rep["parked"] >= 1, "operator/hold parked marker written (encoded)")

# 4b) non-string JSON item_id values: no raise, quarantined, unfenced, idempotent
with tempfile.TemporaryDirectory() as td:
    for label, val in (("int", "7"), ("null", "null"),
                       ("list", "[1]"), ("object", '{"a": 1}')):
        root = Path(td) / f"r-{label}"
        a = DesignAClaimBackend(root)
        a.publish("good-1", b"fine")
        (root / ".items" / "bad.json").write_text(
            '{"item_id": %s, "status": "QUEUED", "payload": "x"}' % val)
        try:
            rep = import_a_state(root)
            raised = None
        except Exception as e:
            raised = e
            rep = {}
        check(raised is None,
              f"item_id={label}: import returns without raising ({raised})")
        # Same class as unreadable JSON: quarantined with payload preserved,
        # then the migration completes — never a crash-abort.
        check(rep.get("unknown", 0) >= 1 and rep.get("verified"),
              f"item_id={label}: record quarantined, migration completes ({rep})")
        q = list((root / "undelivered").glob("*import-unreadable*"))
        check(len(q) == 1 and q[0].read_bytes() != b"",
              f"item_id={label}: quarantine marker preserves the record body")
        try:
            rep2 = import_a_state(root)
            raised2 = None
        except Exception as e:
            raised2, rep2 = e, {}
        check(raised2 is None and rep2.get("noop"),
              f"item_id={label}: re-run is an idempotent no-op ({rep2})")

# 5) counter durability: fsync(temp) precedes replace; dir fsync after
import os
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-fsync"
    a = DesignAClaimBackend(root)
    a.publish("f-1", b"x")
    import_a_state(root)
    calls = []
    _real_fsync, _real_replace = os.fsync, os.replace
    os.fsync = lambda fd: (calls.append("fsync"), _real_fsync(fd))[1]
    os.replace = lambda a_, b_: (calls.append("replace"), _real_replace(a_, b_))[1]
    try:
        dual_read(root, "f-1")               # counter hit -> RMW
    finally:
        os.fsync, os.replace = _real_fsync, _real_replace
    check("fsync" in calls and "replace" in calls,
          f"counter RMW calls both fsync and replace ({calls})")
    if "replace" in calls:
        ri = calls.index("replace")
        check("fsync" in calls[:ri] and "fsync" in calls[ri+1:],
              f"ordering: fsync(temp) BEFORE replace, dir fsync AFTER ({calls})")

# ── reviewer r3 permanent controls (kewei #2, corrupt terminal collision) ─────

# 6) a colliding terminal NAME that fails C's total validator is NOT membership:
#    import fails closed, unfenced, with A state and the collision untouched
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-coll"
    a = DesignAClaimBackend(root)
    a.publish("col-1", b"x")
    tok = a.claim("col-1", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED, provider="P", destination="D")
    arch = root / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / f"{_safe_key('col-1')}.json").write_text("{not json")
    rep = import_a_state(root)
    check(not rep["verified"] and not rep["fenced"]
          and "collision" in rep.get("unmigratable", ""),
          f"corrupt terminal collision fails closed, unfenced ({rep})")
    check(read_epoch(root) == "A" and (root / ".items").is_dir(),
          "collision leaves epoch A and .items intact")
    check((arch / f"{_safe_key('col-1')}.json").read_text() == "{not json",
          "the colliding record's bytes are never touched")
    r = resolve_delivery(root, "col-1")
    check(r["source"] is None and not r["delivered"],
          "unfenced root: no fallback answer is fabricated for the collided id")

# 6b) a NATIVE terminal carries no importer provenance: route match alone
#     proves a DIFFERENT cycle, so the row conflicts (fail closed).
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "r-valid"
    a = DesignAClaimBackend(root)
    a.publish("col-2", b"x")
    tok = a.claim("col-2", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED, provider="P", destination="D")
    c_pre = DesignCClaimBackend(root, activate=True)
    c_pre.publish("col-2", b"x")
    t = c_pre.claim("col-2", "w1")
    c_pre.complete(t, DeliveryOutcome.CONFIRMED, provider="P", destination="D")
    rep = import_a_state(root)
    check(rep.get("conflicts") and not rep["fenced"],
          f"a digest-less native terminal is a conflict, not proof ({rep})")
    check(read_epoch(root) != "C", "valid-collision root stays unfenced")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
