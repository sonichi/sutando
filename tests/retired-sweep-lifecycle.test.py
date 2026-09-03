#!/usr/bin/env python3
"""The retired-inode sweep is a delivery path: it must see every production
claim shape, republish under a name the bridges will claim, carry the body's
own routing, admit one sweeper per inode, and never publish what it cannot
mark as delivered.

Run: python3 tests/retired-sweep-lifecycle.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rd = _load("readiness", "src/delivery/readiness.py")
routing = _load("proactive_routing", "src/proactive_routing.py")

FAILS = 0


def check(label, ok, detail=""):
    global FAILS
    print(f"  {'ok ' if ok else 'FAIL'} {label}" + ("" if ok else f" — {detail}"))
    if not ok:
        FAILS += 1


def retire_with_late(d: Path, name: str, first: str, late: str) -> Path:
    """Claim `name` holding `first`, retire it, then append `late` through a
    descriptor opened before retirement (the producer's stale fd)."""
    claim = d / name
    claim.write_text(first)
    fd = open(claim, "a")
    try:
        assert rd.retire_claim_if_unchanged(claim, first.strip()) is True, "retire refused"
        fd.write(late)
        fd.flush()
    finally:
        fd.close()
    return rd._retired_path(claim)


def late_files(d: Path):
    return sorted(p for p in d.glob("proactive-late-*") if p.name.endswith(".txt"))


with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    # --- production suffix: Slack/Telegram retire `.sending` claims, not `.txt`
    retired = retire_with_late(d, "proactive-42.to-slack.sending", "FIRST\n", "LATE\n")
    check("a .sending retirement lands under retired/", retired.exists() and retired.name.endswith(".sending"))
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("a retired .sending inode is swept", len(pub) == 1, f"published={pub}")
    if pub:
        check("the remainder is the late bytes only", pub[0].read_text().strip() == "LATE", pub[0].read_text())
        check("the typed destination survives in the republished name",
              routing.proactive_destination(pub[0].name) == "slack", pub[0].name)
        check("the name is a bridge-claimable proactive-*.txt",
              pub[0].name.startswith("proactive-") and pub[0].name.endswith(".txt"), pub[0].name)
        check("the source's own proactive- prefix is not repeated in the name",
              pub[0].name.startswith("proactive-late-42-"), pub[0].name)
    check("a second sweep republishes nothing (no duplicate)",
          rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [])
    for f in late_files(d):
        f.unlink()

    # --- .txt positive control (Discord/gateway shape) still sweeps
    retired = retire_with_late(d, "proactive-43.txt", "FIRST\n", "LATE2\n")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check(".txt control: swept", len(pub) == 1 and pub[0].read_text().strip() == "LATE2", f"{pub}")
    if pub:
        check(".txt control: undestined name stays undestined", routing.proactive_destination(pub[0].name) is None, pub[0].name)
    for f in late_files(d):
        f.unlink()

    # --- body-leg redirect is carried onto the remainder
    head = "[channel: 123456789012345678]\nFIRST\n"
    retired = retire_with_late(d, "proactive-44.txt", head, "LATE3\n")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    body = pub[0].read_text() if pub else ""
    check("the delivered head's [channel:] line leads the remainder",
          body.startswith("[channel: 123456789012345678]\n") and body.strip().endswith("LATE3"), repr(body))
    for f in late_files(d):
        f.unlink()

    # --- a remainder without a redirect head carries nothing extra
    retired = retire_with_late(d, "proactive-45.txt", "plain\n", "LATE4\n")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("no redirect head -> bare remainder", pub and pub[0].read_text() == "LATE4\n", f"{[p.read_text() for p in pub]}")
    for f in late_files(d):
        f.unlink()

    # --- one sweeper per inode: a live claim held by another sweeper defers
    retired = retire_with_late(d, "proactive-46.txt", "FIRST\n", "LATE5\n")
    held = retired.with_name(retired.name + rd._SWEEP_SUFFIX)
    os.link(retired, held)
    check("a held inode is skipped by a concurrent sweeper",
          rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [] and not late_files(d))
    # a claim older than the quiesce window is a crashed sweeper's: broken
    old = time.time() - 700
    os.utime(held, (old, old))
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("a stale sweep claim is broken and the inode swept", len(pub) == 1 and not held.exists(), f"{pub} held={held.exists()}")
    for f in late_files(d):
        f.unlink()

    # --- two sweepers racing on one inode publish exactly once
    retired = retire_with_late(d, "proactive-47.txt", "FIRST\n", "LATE6\n")
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        results.append(rd.sweep_retired(d, quiesce_s=600, now=time.time()))

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    total = sum(len(r) for r in results)
    check("racing sweepers publish exactly one remainder", total == 1 and len(late_files(d)) == 1,
          f"results={results} files={late_files(d)}")
    for f in late_files(d):
        f.unlink()

    # --- marker creation failure at retirement fails closed (claim kept)
    claim = d / "proactive-48.txt"
    claim.write_text("FIRST\n")
    real_write = rd._write_atomic
    rd._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError("marker disk full"))
    try:
        ok = rd.retire_claim_if_unchanged(claim, "FIRST")
    finally:
        rd._write_atomic = real_write
    check("unwritable delivered-marker -> retire returns False and the claim is restored",
          ok is False and claim.exists() and not rd._retired_path(claim).exists(),
          f"ok={ok} claim={claim.exists()} retired={rd._retired_path(claim).exists()}")
    claim.unlink()

    # --- marker advance failure during the sweep publishes nothing, then retries once
    retired = retire_with_late(d, "proactive-49.txt", "FIRST\n", "LATE7\n")
    marker = rd._delivered_marker(retired)
    before = marker.read_text()
    rd._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError("marker disk full"))
    try:
        pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    finally:
        rd._write_atomic = real_write
    check("marker advance failure -> nothing published, marker unchanged, no scratch left",
          pub == [] and not late_files(d) and marker.read_text() == before
          and not [p for p in d.iterdir() if p.name.startswith(".proactive-late-")],
          f"pub={pub} files={late_files(d)} marker={marker.read_text()!r}")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("after the marker is writable again the remainder is published once",
          len(pub) == 1 and pub[0].read_text().strip() == "LATE7", f"{pub}")
    check("and not again", rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [])
    for f in late_files(d):
        f.unlink()

    # --- a legacy retired inode (no marker) is never republished, only aged out
    legacy = d / "retired" / "proactive-50.to-telegram.sending"
    legacy.write_text("old\nbytes\n")
    check("legacy .sending retirement without a marker is not republished",
          rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [] and legacy.exists())
    check("and is aged out once quiescent",
          rd.sweep_retired(d, quiesce_s=0, now=time.time() + 1) == [] and not legacy.exists())

    # --- markers and scratch names are never treated as inodes
    (d / "retired" / "proactive-51.txt.delivered").write_text("6")
    (d / "retired" / ".proactive-52.txt.delivered.abc.tmp").write_text("6")
    check("marker/scratch names are not swept as inodes",
          rd.sweep_retired(d, quiesce_s=0, now=time.time()) == [])

print(f"\n{'FAILED' if FAILS else 'OK'} — {FAILS} failure(s)")
sys.exit(1 if FAILS else 0)
