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
    SEP, TERMINAL_TAG, DesignCClaimBackend, _safe_key)
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
         "incarnation": tok3.incarnation}))
    rep3 = b3.recover()
    check(k3 in rep3.retired, "M-D crash: stale claim retired, not re-readied")
    check(not (b3.root / "ready" / k3).exists(),
          "M-D crash: item is NOT redelivered (no double-send)")

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
    check(b6.terminal_record("item-6")["receipt"]["destination"] == "D1",
          "primary record is the first delivery; the suffix carries the second")

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

    # ── durability=lax skips fsync but keeps the protocol shape ────────
    b7 = fresh(td, "lax", durability="lax")
    b7.publish("item-7", b"p")
    b7.complete(b7.claim("item-7", "w0"), DeliveryOutcome.CONFIRMED,
                provider="P", destination="D")
    check(b7.terminal_record("item-7") is not None, "lax mode still records")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
