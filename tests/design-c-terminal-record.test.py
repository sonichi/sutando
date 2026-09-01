#!/usr/bin/env python3
"""Gate ③ of the C-canonical ruling: receipt-aware terminal records.

Each crash window from the design's recovery decision table is a CONSTRUCTED
on-disk state (no kills, no sleeps): build the state a crash would leave, run
recover(), assert the prescribed action — finalize (R-M), retire (M-D), and
the legacy-archive and live-holder rows stay untouched.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    SEP, TERMINAL_TAG, TMP, DesignCClaimBackend, _safe_component, _safe_key)
from ag2_sparrow.delivery_core.contract import DeliveryOutcome  # noqa: E402

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def fresh(td, name="root", **kw):
    return DesignCClaimBackend(Path(td) / name, activate=True, **kw)


with tempfile.TemporaryDirectory() as td:
    # ── receipt round-trip ──────────────────────────────────────────────
    b = fresh(td, "rt")
    b.publish("item-1", b"payload")
    tok = b.claim("item-1", "w0")
    ok = b.complete(tok, DeliveryOutcome.CONFIRMED,
                    provider="DiscordDeliveryProvider", destination="chan-123")
    check(ok, "confirmed complete succeeds")
    rec = b.terminal_record("item-1")
    check(rec is not None, "terminal record exists after CONFIRMED")
    check(rec["receipt"] == {"provider": "DiscordDeliveryProvider",
                             "destination": "chan-123"},
          "receipt fields persist verbatim")
    check(rec["outcome"] == "confirmed" and rec["worker"] == "w0",
          "outcome and worker recorded")
    check(b.persists_receipt_metadata is True,
          "persists_receipt_metadata is now True for C")
    key = _safe_key("item-1")
    check(not (b.root / "inflight" / tok.incarnation).exists(),
          "claim released (D happened)")
    check(b.claim("item-1", "w9") is None or True, "no crash re-claiming")

    # ── R-M window: tmp terminal present, claim still held ─────────────
    b2 = fresh(td, "rm")
    b2.publish("item-2", b"p")
    tok2 = b2.claim("item-2", "w0")
    k2 = _safe_key("item-2")
    record = {"schema": 1, "item_id": "item-2", "outcome": "confirmed",
              "receipt": {"provider": "P", "destination": "D"},
              "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
              "incarnation": tok2.incarnation}
    tmp = b2.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tok2.incarnation}{SEP}{time.time_ns()}.json"
    tmp.write_text(json.dumps(record))
    rep = b2.recover()
    check(k2 in rep.retired, "R-M crash: recover reports the finalization")
    check(b2.terminal_record("item-2") is not None,
          "R-M crash: tmp record finalized into archive (no provider re-ask)")
    check(not tmp.exists(), "R-M crash: tmp record consumed")
    rep2 = b2.recover()
    check(not (b2.root / "inflight" / tok2.incarnation).exists(),
          "and the stale claim is retired on the terminal key")

    # ── M-D window: archive record durable, claim still held ───────────
    b3 = fresh(td, "md")
    b3.publish("item-3", b"p")
    tok3 = b3.claim("item-3", "w0")
    k3 = _safe_key("item-3")
    (b3.root / "archive" / f"{k3}.json").write_text(json.dumps(
        {"schema": 1, "item_id": "item-3", "outcome": "confirmed",
         "receipt": {"provider": "P", "destination": "D"},
         "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
         "incarnation": tok3.incarnation}))
    rep3 = b3.recover()
    check(k3 in rep3.retired, "M-D crash: stale claim retired, not re-readied")
    check(not (b3.root / "ready" / k3).exists(),
          "M-D crash: item is NOT redelivered (no double-send)")

    # ── REVIEW CONTROL: a malformed archive record must not retire a
    #    live claim (fail closed — the item would be silently lost) ──────
    b3b = fresh(td, "md-malformed")
    b3b.publish("item-3b", b"p")
    tok3b = b3b.claim("item-3b", "w0")
    k3b = _safe_key("item-3b")
    (b3b.root / "archive" / f"{k3b}.json").write_text(json.dumps(
        {"incarnation": tok3b.incarnation}))
    rep3b = b3b.recover()
    check(k3b not in rep3b.retired,
          "malformed archive record does NOT authorize retirement")
    check((b3b.root / "inflight" / tok3b.incarnation).exists(),
          "the live claim survives a malformed archive record")

    # ── legacy filename-format archive entries: OWN incarnation retires,
    #    a FOREIGN one never touches a live claim ─────────────────────────
    b4 = fresh(td, "legacy")
    b4.publish("item-4", b"p")
    tok4 = b4.claim("item-4", "w0")
    k4 = _safe_key("item-4")
    (b4.root / "archive" / f"{k4}{SEP}w9{SEP}1{SEP}2{SEP}3{SEP}9").write_text("")
    rep4 = b4.recover()
    check((b4.root / "inflight" / tok4.incarnation).exists(),
          "a FOREIGN legacy entry does not retire the live claim")
    (b4.root / "archive" / f"{tok4.incarnation}{SEP}9").write_text("")
    rep4b = b4.recover()
    check(k4 in rep4b.retired,
          "the claim's OWN legacy entry retires it (no redrive)")
    check(b4.terminal_record("item-4") is None,
          "legacy entries yield no receipt (terminal-without-receipt)")

    # ── live holder is never touched by the new rows ───────────────────
    b5 = fresh(td, "live")
    b5.publish("item-5", b"p")
    tok5 = b5.claim("item-5", "w0")   # THIS process holds it — live by pid
    rep5 = b5.recover()
    check((b5.root / "inflight" / tok5.incarnation).exists(),
          "live holder's claim survives recover()")

    # ── redelivery of a completed id keeps BOTH records ────────────────
    b6 = fresh(td, "redeliver")
    b6.publish("item-6", b"p")
    b6.complete(b6.claim("item-6", "w0"), DeliveryOutcome.CONFIRMED,
                provider="P", destination="D1")
    b6.publish("item-6", b"p2")
    t6 = b6.claim("item-6", "w0")
    check(t6 is not None, "re-publish after terminal is claimable")
    b6.complete(t6, DeliveryOutcome.CONFIRMED, provider="P", destination="D2")
    k6 = _safe_key("item-6")
    recs = [f for f in (b6.root / "archive").iterdir() if f.suffix == ".json"]
    check(len(recs) == 2, f"both terminal records kept (got {len(recs)})")
    check(b6.terminal_record("item-6")["receipt"]["destination"] == "D2",
          "terminal_record returns the LATEST cycle (D2), not the first")
    hist = b6.terminal_records("item-6")
    check([r["receipt"]["destination"] for r in hist] == ["D1", "D2"],
          "terminal_records lists the full history oldest-first")

    # ── REVIEW CONTROL: older terminal must NOT retire a LIVE redelivery ─
    b8 = fresh(td, "redeliver-live")
    b8.publish("same", b"p")
    b8.complete(b8.claim("same", "w0"), DeliveryOutcome.CONFIRMED,
                provider="P", destination="D1")
    b8.publish("same", b"p2")
    t_live = b8.claim("same", "w0")          # live claimant for D2
    rep8 = b8.recover()
    check((b8.root / "inflight" / t_live.incarnation).exists(),
          "older terminal does not retire the live redelivery claim")
    ok8 = b8.complete(t_live, DeliveryOutcome.CONFIRMED,
                      provider="P", destination="D2")
    check(ok8, "the second delivery completes after recover()")
    k8 = _safe_key("same")
    recs8 = sorted(f for f in (b8.root / "archive").iterdir() if f.suffix == ".json")
    check(len(recs8) == 2, f"both deliveries' receipts persist (got {len(recs8)})")

    # ── REVIEW CONTROL: a torn staging temp is never finalized ─────────
    b9 = fresh(td, "torn")
    b9.publish("item-9", b"p")
    tok9 = b9.claim("item-9", "w0")
    torn = b9.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tok9.incarnation}{SEP}1.json"
    torn.write_text("{")                      # crash mid-write shape
    rep9 = b9.recover()
    check(b9.terminal_record("item-9") is None,
          "torn staging temp is not promoted to a terminal record")
    check(not torn.exists(), "and the torn temp is cleaned up")

    # ── strict recovery carries the SAME barrier as strict complete ────
    fsyncs = []
    _real_fsync = os.fsync
    b10 = fresh(td, "strict-rec", durability="strict")
    b10.publish("item-10", b"p")
    tok10 = b10.claim("item-10", "w0")
    rec10 = {"schema": 1, "item_id": "item-10", "outcome": "confirmed",
             "receipt": {"provider": "P", "destination": "D"},
             "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
             "incarnation": tok10.incarnation}
    (b10.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tok10.incarnation}{SEP}2.json").write_text(
        json.dumps(rec10))
    os.fsync = lambda fd: (fsyncs.append(fd), _real_fsync(fd))[1]
    try:
        b10.recover()
    finally:
        os.fsync = _real_fsync
    check(len(fsyncs) >= 2,
          f"strict recovery fsyncs record AND archive dir before release ({len(fsyncs)})")
    check(b10.terminal_record("item-10") is not None,
          "and the staged record finalized")
    check(not (b10.root / "inflight" / tok10.incarnation).exists(),
          "and the claim released only after the barrier")

    # ── INCOMPLETE staged records are torn, never promoted ─────────────
    b11 = fresh(td, "schema-only")
    b11.publish("victim", b"p")
    tok11 = b11.claim("victim", "w0")
    (b11.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tok11.incarnation}{SEP}3.json").write_text(
        '{"schema": 1}')
    b11.recover()
    check(b11.terminal_record("victim") is None,
          "schema-only staged JSON is torn — not promoted")
    check((b11.root / "inflight" / tok11.incarnation).exists()
          or (b11.root / "ready" / _safe_key("victim")).exists(),
          "and the delivery is NOT lost (claim intact or re-readied)")
    b12 = fresh(td, "foreign-inc")
    b12.publish("item-12", b"p")
    tok12 = b12.claim("item-12", "w0")
    full = {"schema": 1, "item_id": "item-12", "outcome": "confirmed",
            "receipt": {"provider": "P", "destination": "D"},
            "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
            "incarnation": "someone~else~1~2~3"}
    (b12.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tok12.incarnation}{SEP}4.json").write_text(
        json.dumps(full))
    b12.recover()
    check(b12.terminal_record("item-12") is None,
          "a record bound to a FOREIGN incarnation is torn for this staging name")
    check((b12.root / "inflight" / tok12.incarnation).exists()
          or (b12.root / "ready" / _safe_key("item-12")).exists(),
          "and that delivery is not lost either")

    # ── non-confirmed outcome / null receipt are torn too ──────────────
    b13 = fresh(td, "retryable")
    b13.publish("item-13", b"p")
    tok13 = b13.claim("item-13", "w0")
    bad = {"schema": 1, "item_id": "item-13", "outcome": "retryable",
           "receipt": None, "completed_ns": time.time_ns(), "worker": "w0",
           "attempts": 0, "incarnation": tok13.incarnation}
    (b13.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tok13.incarnation}{SEP}5.json").write_text(
        json.dumps(bad))
    b13.recover()
    check(b13.terminal_record("item-13") is None,
          "outcome=retryable + receipt=null is torn — never promoted")
    check((b13.root / "inflight" / tok13.incarnation).exists()
          or (b13.root / "ready" / _safe_key("item-13")).exists(),
          "and the delivery survives")

    # ── REVIEW CONTROL: legal SHORT writes still persist the full record ─
    from unittest import mock
    b14 = fresh(td, "short-write")
    b14.publish("item-14", b"p")
    tok14 = b14.claim("item-14", "w0")
    _real_write = os.write
    with mock.patch.object(os, "write",
                           side_effect=lambda fd, d: _real_write(fd, d[:7])):
        ok14 = b14.complete(tok14, DeliveryOutcome.CONFIRMED,
                            provider="P", destination="D")
    check(ok14, "complete() succeeds under 7-byte short writes")
    rec14 = b14.terminal_record("item-14")
    check(rec14 is not None and rec14["receipt"]["destination"] == "D",
          "short writes: the archived record is COMPLETE, not truncated")
    check(not (b14.root / "inflight" / tok14.incarnation).exists(),
          "and the claim released only after the full record landed")

    # ── REVIEW CONTROL: crash MID-write keeps the claim (no lost proof) ─
    b15 = fresh(td, "midwrite-crash")
    b15.publish("item-15", b"p")
    tok15 = b15.claim("item-15", "w0")
    calls = {"n": 0}

    def _one_then_crash(fd, d):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated crash mid-write")
        return _real_write(fd, d[:7])
    try:
        with mock.patch.object(os, "write", side_effect=_one_then_crash):
            b15.complete(tok15, DeliveryOutcome.CONFIRMED,
                         provider="P", destination="D")
        crashed = False
    except OSError:
        crashed = True
    check(crashed, "mid-write crash propagates out of complete()")
    check((b15.root / "inflight" / tok15.incarnation).exists(),
          "mid-write crash: the claim REMAINS held (release never ran)")
    check(b15.terminal_record("item-15") is None,
          "mid-write crash: no truncated record was finalized")
    b15.recover()
    check(not list((b15.root / "tmp").glob(f"{TERMINAL_TAG}{SEP}*.json")),
          "recover() deletes the torn staging temp")
    check((b15.root / "inflight" / tok15.incarnation).exists(),
          "and the LIVE holder's claim survives recovery")

    # ── REVIEW CONTROLS: malformed staged fields never abort recovery ──
    for label, field, bad in (("int item_id", "item_id", 7),
                              ("empty worker", "worker", "")):
        bx = fresh(td, f"malformed-{field}")
        bx.publish("item-x", b"p")
        tokx = bx.claim("item-x", "w0")
        recx = {"schema": 1, "item_id": "item-x", "outcome": "confirmed",
                "receipt": {"provider": "P", "destination": "D"},
                "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
                "incarnation": tokx.incarnation, field: bad}
        staged = bx.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tokx.incarnation}{SEP}1.json"
        staged.write_text(json.dumps(recx))
        aborted = False
        try:
            bx.recover()
            bx.recover()                     # a second pass must not wedge either
        except (TypeError, ValueError):
            aborted = True
        check(not aborted, f"{label}: recovery passes complete without raising")
        check(not staged.exists(), f"{label}: the malformed staging record is deleted")
        check((bx.root / "inflight" / tokx.incarnation).exists()
              or (bx.root / "ready" / _safe_key("item-x")).exists(),
              f"{label}: the delivery is not lost")

    # ── REVIEW CONTROL: publish("") is contract-legal; its staged terminal
    #    must FINALIZE, not be deleted-and-redelivered (double-send) ──────
    be = fresh(td, "empty-id")
    be.publish("", b"p")
    toke = be.claim("", "w0")
    ke = _safe_key("")
    rece = {"schema": 1, "item_id": "", "outcome": "confirmed",
            "receipt": {"provider": "P", "destination": "D"},
            "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
            "incarnation": toke.incarnation}
    stg = be.root / "tmp" / f"{TERMINAL_TAG}{SEP}{toke.incarnation}{SEP}1.json"
    stg.write_text(json.dumps(rece))
    repe = be.recover()
    check(ke in repe.retired, 'empty item_id: R-M staged record finalizes')
    check(be.terminal_record("") is not None,
          'empty item_id: terminal record preserved (not deleted)')
    check(not (be.root / "ready" / ke).exists(),
          'empty item_id: NOT re-armed for redelivery')

    # ── ROUND-4 CONTROLS (keweichen) ───────────────────────────────────
    # 1. Strict M-D recovery barriers the archive dir BEFORE the claim dies.
    b20 = fresh(td, "strict-md", durability="strict")
    b20.publish("item-20", b"p")
    tok20 = b20.claim("item-20", "w0")
    k20 = _safe_key("item-20")
    (b20.root / "archive" / f"{k20}.json").write_text(json.dumps(
        {"schema": 1, "item_id": "item-20", "outcome": "confirmed",
         "receipt": {"provider": "P", "destination": "D"},
         "completed_ns": time.time_ns(), "worker": "w0", "attempts": 0,
         "incarnation": tok20.incarnation}))
    barriers = []
    orig_barrier = b20._strict_dir_barrier
    b20._strict_dir_barrier = lambda: barriers.append(
        (b20.root / "inflight" / tok20.incarnation).exists()) or orig_barrier()
    rep20 = b20.recover()
    b20._strict_dir_barrier = orig_barrier
    check(k20 in rep20.retired, "strict M-D: claim retired")
    check(len(barriers) >= 1 and barriers[0] is True,
          "strict M-D: archive dir barrier fires BEFORE the claim unlink")

    # 2a. An empty receipt {} never authorizes retirement nor reads as proof.
    b21 = fresh(td, "empty-receipt")
    b21.publish("item-21", b"p")
    tok21 = b21.claim("item-21", "w0")
    k21 = _safe_key("item-21")
    (b21.root / "archive" / f"{k21}.json").write_text(json.dumps(
        {"schema": 1, "item_id": "item-21", "outcome": "confirmed",
         "receipt": {}, "completed_ns": time.time_ns(), "worker": "w0",
         "attempts": 0, "incarnation": tok21.incarnation}))
    rep21 = b21.recover()
    check(k21 not in rep21.retired
          and (b21.root / "inflight" / tok21.incarnation).exists(),
          "empty receipt: live claim survives (not writer-produced)")
    check(b21.terminal_record("item-21") is None,
          "empty receipt: never returned as terminal proof")

    # 2b. A malformed record beside a valid one: reads return the valid one
    #     and never raise on unsortable metadata.
    b22 = fresh(td, "malformed-read")
    b22.publish("item-22", b"p")
    b22.complete(b22.claim("item-22", "w0"), DeliveryOutcome.CONFIRMED,
                 provider="P", destination="D")
    k22 = _safe_key("item-22")
    (b22.root / "archive" / f"{k22}{SEP}999.json").write_text(json.dumps(
        {"schema": 1, "item_id": "item-22", "outcome": "confirmed",
         "receipt": {"provider": "P", "destination": "D"},
         "completed_ns": "newest", "worker": "w0", "attempts": 0,
         "incarnation": "bogus"}))
    try:
        rec22 = b22.terminal_record("item-22")
        raised = False
    except TypeError:
        rec22, raised = None, True
    check(not raised, "malformed completed_ns: read does not raise")
    check(rec22 is not None and rec22["receipt"]["provider"] == "P",
          "malformed record beside valid: the VALID one is returned")

    # 3. A token whose item_id does not bind to the incarnation key is
    #    refused before any mutation.
    b23 = fresh(td, "unbound-token")
    b23.publish("item-23", b"p")
    tok23 = b23.claim("item-23", "w0")
    from ag2_sparrow.delivery_core.contract import ClaimToken
    forged = ClaimToken(item_id="different-item", worker="w0",
                        incarnation=tok23.incarnation)
    ok23 = b23.complete(forged, DeliveryOutcome.CONFIRMED,
                        provider="P", destination="D")
    check(ok23 is False, "unbound token: complete() refuses")
    check((b23.root / "inflight" / tok23.incarnation).exists(),
          "unbound token: claim intact, nothing archived")
    check(b23.terminal_record("different-item") is None
          and b23.terminal_record("item-23") is None,
          "unbound token: no unfindable receipt was written")

    # ── ROUND-5 CONTROLS: only the EXACT legacy grammar is evidence ────
    for label, maker in (
        (".partial file", lambda r, inc: (r / "archive" / f"{inc}.partial").write_text("torn")),
        ("directory sharing prefix", lambda r, inc: (r / "archive" / f"{inc}{SEP}123").mkdir()),
        ("non-numeric suffix", lambda r, inc: (r / "archive" / f"{inc}{SEP}xyz").write_text("")),
        ("exact-grammar SYMLINK", lambda r, inc: ((r / "outside.txt").write_text("x"), (r / "archive" / f"{inc}{SEP}123").symlink_to(r / "outside.txt"))),
    ):
        bl = fresh(td, f"legacy-{label.split()[0].strip('.')}")
        bl.publish("item-L", b"p")
        tokL = bl.claim("item-L", "w0")
        maker(bl.root, tokL.incarnation)
        repL = bl.recover()
        check((bl.root / "inflight" / tokL.incarnation).exists(),
              f"legacy grammar: {label} never retires the live claim")

    # ── interrupted QUARANTINE (link done, unlink not): finish it,
    #    never re-ready — UNKNOWN must not become a redelivery ─────────────
    b30 = fresh(td, "quarantine-crash")
    b30.publish("item-30", b"p")
    tok30 = b30.claim("item-30", "w0")
    k30 = _safe_key("item-30")
    und30 = b30.root / "undelivered" / f"{k30}{SEP}outcome-unknown{SEP}123"
    os.link(str(b30.root / "inflight" / tok30.incarnation), str(und30))
    p30 = tok30.incarnation.split(SEP)
    dead30 = f"{p30[0]}{SEP}{p30[1]}{SEP}99999{SEP}1{SEP}{p30[4]}"
    os.rename(str(b30.root / "inflight" / tok30.incarnation),
              str(b30.root / "inflight" / dead30))
    rep30 = b30.recover()
    check(k30 in rep30.quarantined and k30 not in rep30.recovered,
          "interrupted quarantine is FINISHED by recovery, not re-readied")
    check(not (b30.root / "ready" / k30).exists() and und30.exists(),
          "UNKNOWN item stays quarantined (no redelivery of a maybe-received item)")

    # ── malformed quarantine entries never wedge or spoof recovery ──────
    for label, maker, expect_requeue in (
        ("dangling symlink", lambda r, k: (r / "undelivered" / f"{k}{SEP}dang{SEP}1").symlink_to(r / "gone"), True),
        ("directory entry", lambda r, k: (r / "undelivered" / f"{k}{SEP}dir{SEP}1").mkdir(), True),
        ("unrelated regular file", lambda r, k: (r / "undelivered" / f"{k}{SEP}other{SEP}1").write_text("x"), True),
    ):
        bq = fresh(td, f"qmal-{label.split()[0]}")
        bq.publish("item-Q", b"p")
        tq = bq.claim("item-Q", "w0")
        kq = _safe_key("item-Q")
        maker(bq.root, kq)
        pq = tq.incarnation.split(SEP)
        deadq = f"{pq[0]}{SEP}{pq[1]}{SEP}99999{SEP}1{SEP}{pq[4]}"
        os.rename(str(bq.root / "inflight" / tq.incarnation),
                  str(bq.root / "inflight" / deadq))
        try:
            repq = bq.recover()
            raised = False
        except OSError:
            repq, raised = None, True
        check(not raised, f"quarantine {label}: recover() never raises")
        check(repq is not None and (kq in repq.recovered) == expect_requeue,
              f"quarantine {label}: dead claim re-readied (not a real twin)")

    # ── ROUND-8 CONTROL: twin identity is (st_dev, st_ino) — a same-inode
    #    entry on ANOTHER filesystem re-readies the dead claim, never twins ──
    bxd = fresh(td, "qxdev")
    bxd.publish("item-X", b"p")
    txd = bxd.claim("item-X", "w0")
    kxd = _safe_key("item-X")
    xdev_name = f"{kxd}{SEP}xdev{SEP}1"
    (bxd.root / "undelivered" / xdev_name).write_text("unrelated")
    pxd = txd.incarnation.split(SEP)
    deadxd = f"{pxd[0]}{SEP}{pxd[1]}{SEP}99999{SEP}1{SEP}{pxd[4]}"
    os.rename(str(bxd.root / "inflight" / txd.incarnation),
              str(bxd.root / "inflight" / deadxd))
    claim_st = os.lstat(str(bxd.root / "inflight" / deadxd))
    _real_lstat = os.lstat

    def _xdev_lstat(path, *a, **kw):
        st = _real_lstat(path, *a, **kw)
        if str(path).endswith(xdev_name):
            # Same st_ino as the dead claim, different st_dev: the cross-
            # device shape an inode-only comparison misclassifies as a twin.
            return os.stat_result((st.st_mode, claim_st.st_ino,
                                   claim_st.st_dev + 1, st.st_nlink,
                                   st.st_uid, st.st_gid, st.st_size,
                                   st.st_atime, st.st_mtime, st.st_ctime))
        return st

    os.lstat = _xdev_lstat
    try:
        repxd = bxd.recover()
    finally:
        os.lstat = _real_lstat
    check(kxd in repxd.recovered and kxd not in repxd.quarantined,
          "same-inode/different-device entry is NOT a twin: dead claim re-readied")
    check((bxd.root / "ready" / kxd).exists(),
          "cross-device false-twin: the item is deliverable again, not suppressed")

    # ── durability=lax skips fsync but keeps the protocol shape ────────
    b7 = fresh(td, "lax", durability="lax")
    b7.publish("item-7", b"p")
    b7.complete(b7.claim("item-7", "w0"), DeliveryOutcome.CONFIRMED,
                provider="P", destination="D")
    check(b7.terminal_record("item-7") is not None, "lax mode still records")


    # ── retirement clears the attempt budget on EVERY path: freeing the
    # claim but keeping attempts/{key} parks the next cycle on refusal #1.
    def burn(b, item, n=2):
        b.publish(item, b"p")
        for _ in range(n):
            b.complete(b.claim(item, "w0"), DeliveryOutcome.NOT_DELIVERED,
                       park_at_attempts=3)
        return _safe_key(item)

    def next_cycle_parks(b, item):
        """Republish and fail ONCE. True => the fresh cycle parked early."""
        b.publish(item, b"p")
        b.complete(b.claim(item, "w0"), DeliveryOutcome.NOT_DELIVERED,
                   park_at_attempts=3)
        return not (b.root / "ready" / _safe_key(item)).exists()

    # CONTROL — the normal path already gets this right.
    bn = fresh(td, "budget-normal")
    burn(bn, "item-n")
    bn.complete(bn.claim("item-n", "w0"), DeliveryOutcome.CONFIRMED,
                provider="P", destination="D")
    check(bn.attempts("item-n") == 0, "control: normal confirm clears the budget")
    check(not next_cycle_parks(bn, "item-n"),
          "control: the next cycle gets its full budget")

    # R-M retirement.
    br = fresh(td, "budget-rm")
    burn(br, "item-r")
    tr = br.claim("item-r", "w0")
    (br.root / "tmp" / f"{TERMINAL_TAG}{SEP}{tr.incarnation}{SEP}{time.time_ns()}.json").write_text(
        json.dumps({"schema": 1, "item_id": "item-r", "outcome": "confirmed",
                    "receipt": {"provider": "P", "destination": "D"},
                    "completed_ns": time.time_ns(), "worker": "w0",
                    "attempts": 2, "incarnation": tr.incarnation}))
    br.recover()
    check(br.attempts("item-r") == 0,
          f"R-M retirement clears the spent budget (got {br.attempts('item-r')})")
    check(not next_cycle_parks(br, "item-r"),
          "R-M: a republished item gets its full budget, not the prior cycle's")

    # M-D retirement — archive record durable, claim still held.
    bm = fresh(td, "budget-md")
    burn(bm, "item-m")
    tm = bm.claim("item-m", "w0")
    bm._write_terminal(_safe_key("item-m"),
                       {"schema": 1, "item_id": "item-m", "outcome": "confirmed",
                        "receipt": {"provider": "P", "destination": "D"},
                        "completed_ns": time.time_ns(), "worker": "w0",
                        "attempts": 2, "incarnation": tm.incarnation},
                       tm.incarnation)
    bm.recover()
    check(bm.attempts("item-m") == 0,
          f"M-D retirement clears the spent budget (got {bm.attempts('item-m')})")
    check(not next_cycle_parks(bm, "item-m"),
          "M-D: a republished item gets its full budget")


    # ── bool subclasses int and True == 1, so an equality gate admits records
    # _write_terminal cannot emit; and a symlink's bytes live outside the store.
    def rec(**over):
        r = {"schema": 1, "item_id": "item-b", "outcome": "confirmed",
             "receipt": {"provider": "P", "destination": "D"},
             "completed_ns": 123, "worker": "w0", "attempts": 0,
             "incarnation": SEP.join((_safe_key('item-b'), _safe_component('w0'),
                                      "1", "2", "3"))}
        r.update(over)
        return r

    bv = fresh(td, "bool-valid")
    check(bv._record_is_terminal_proof(rec()) is True, "control: a real record validates")
    # Arity is policy: 5-part native or 2-part import ONLY. A 3-part
    # collision passed the validator and satisfied importer membership.
    _k, _w = _safe_key('item-b'), _safe_component('w0')
    check(bv._record_is_terminal_proof(
        rec(incarnation=SEP.join((_k, _w, "1")))) is False,
        "a 3-part incarnation is rejected — no writer emits it")
    check(bv._record_is_terminal_proof(
        rec(incarnation=SEP.join((_k, _w, "notapid", "b", "n")))) is False,
        "a 5-part incarnation with a non-numeric pid is rejected")
    # The importer is the only 2-part writer, so the 2-part arm demands its
    # exact provenance; a bare key+worker JSON is no writer's output.
    check(bv._record_is_terminal_proof(
        rec(incarnation=SEP.join((_k, _w)))) is False,
        "a 2-part shape WITHOUT importer provenance is rejected")
    _ai = _safe_component("a-import")
    _imp = dict(worker="a-import", imported=True,
                a_record_digest="ab" * 32,
                incarnation=SEP.join((_k, _ai)))
    check(bv._record_is_terminal_proof(rec(**_imp)) is True,
          "control: the genuine imported 2-part shape validates")
    for miss in ("imported", "a_record_digest"):
        broken = {k: v for k, v in _imp.items() if k != miss}
        check(bv._record_is_terminal_proof(rec(**broken)) is False,
              f"a 2-part record missing {miss} is rejected")
    check(bv._record_is_terminal_proof(
        rec(**{**_imp, "imported": 1})) is False,
        "imported=1 (not True) is rejected on the 2-part arm")
    check(bv._record_is_terminal_proof(
        rec(**{**_imp, "a_record_digest": "AB" * 32})) is False,
        "an uppercase-hex digest is rejected (writer emits lowercase)")
    check(bv._record_is_terminal_proof(
        rec(**{**_imp, "a_record_digest": "ab" * 31})) is False,
        "a short digest is rejected")
    for field in ("schema", "completed_ns", "attempts"):
        for val in (True, False):
            check(bv._record_is_terminal_proof(rec(**{field: val})) is False,
                  f"{field}={val} is rejected (bool is not an exact int)")

    # A staged SYMLINK named like valid proof must not be promoted, and must
    # not retire the live claim — deleting its target would erase the proof.
    bs = fresh(td, "sym-staged")
    bs.publish("item-s", b"p")
    ts_ = bs.claim("item-s", "w0")
    ks = _safe_key("item-s")
    outside = Path(td) / "outside-proof.json"
    outside.write_text(json.dumps(rec(item_id="item-s", incarnation=ts_.incarnation)))
    link = bs.root / "tmp" / f"{TERMINAL_TAG}{SEP}{ts_.incarnation}{SEP}{time.time_ns()}.json"
    link.symlink_to(outside)
    rep_s = bs.recover()
    check(ks not in rep_s.retired, f"a staged SYMLINK does not retire the claim ({rep_s.retired})")
    check(bs.terminal_record("item-s") is None,
          "and it is not promoted into the archive as proof")

    # Same for a symlink already sitting in the archive: never read as proof.
    ba = fresh(td, "sym-archive")
    ba.publish("item-a2", b"p")
    ka = _safe_key("item-a2")
    outside2 = Path(td) / "outside-archive.json"
    outside2.write_text(json.dumps(rec(
        item_id="item-a2",
        incarnation=f"{_safe_key('item-a2')}{SEP}{_safe_component('w0')}{SEP}1")))
    (ba.root / "archive" / f"{ka}.json").symlink_to(outside2)
    check(ba.terminal_record("item-a2") is None,
          "an ARCHIVE symlink is not returned as a terminal record")
    check(ba.terminal_records("item-a2") == [],
          "and terminal_records() lists nothing for it")


    # ── a clock correction must not let an older receipt win: regressing
    # time.time_ns() between cycles used to return the FIRST one's destination.
    import ag2_sparrow.delivery_core.backend_c as _bc
    bclk = fresh(td, "clockback")
    real_ns = _bc.time.time_ns
    ticks = iter([2_000_000_000, 1_999_999_999])   # second cycle is EARLIER

    def cycle(dest, forced_ns):
        bclk.publish("item-clk", b"p")
        tok = bclk.claim("item-clk", "w0")
        _bc.time.time_ns = lambda: forced_ns
        try:
            bclk.complete(tok, DeliveryOutcome.CONFIRMED, provider="P", destination=dest)
        finally:
            _bc.time.time_ns = real_ns

    cycle("D1", next(ticks))
    cycle("D2", next(ticks))          # newer cycle, EARLIER timestamp

    recs = bclk.terminal_records("item-clk")
    hist = [(r["completed_ns"], r["receipt"]["destination"]) for r in recs]
    check(len(recs) == 2, f"both cycles recorded ({len(recs)})")
    check(hist[-1][1] == "D2",
          f"the LATER cycle wins despite the earlier clock: history={hist}")
    check(bclk.terminal_record("item-clk")["receipt"]["destination"] == "D2",
          "terminal_record() returns the current cycle's destination")
    check([r["cycle"] for r in recs] == [1, 2],
          f"cycles are logical and monotonic ({[r.get('cycle') for r in recs]})")

with tempfile.TemporaryDirectory() as td:
    # A terminal record staged in tmp/ but not yet renamed into archive/ is
    # already authoritative for ordering: a re-derived cycle must not reuse it.
    bs = fresh(td, "staged")
    bs.publish("item-stg", b"p")
    tok_s = bs.claim("item-stg", "w-stg")
    key_s = _safe_key("item-stg")
    check(bs._next_cycle(key_s) == 1, "baseline cycle is 1 on an empty store")

    staged = (bs._d(TMP)
              / f"{TERMINAL_TAG}{SEP}{tok_s.incarnation}{SEP}1787850000000001.json")
    staged.write_text(json.dumps({"cycle": 7, "incarnation": tok_s.incarnation}))
    check(bs._next_cycle(key_s) == 8,
          f"a cycle=7 record staged in tmp/ advances the next cycle to 8 "
          f"(got {bs._next_cycle(key_s)})")

    # Control: the scan is key-scoped, not a store-wide max — another item's
    # staged record must not move this key's cycle.
    bs.publish("item-other", b"p")
    tok_o = bs.claim("item-other", "w-oth")
    (bs._d(TMP) / f"{TERMINAL_TAG}{SEP}{tok_o.incarnation}{SEP}1787850000000002.json"
     ).write_text(json.dumps({"cycle": 99, "incarnation": tok_o.incarnation}))
    check(bs._next_cycle(key_s) == 8,
          f"another key's staged cycle=99 does NOT move this key "
          f"(got {bs._next_cycle(key_s)})")
    check(bs._next_cycle(_safe_key("item-other")) == 100,
          "...and it does move its own key")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
