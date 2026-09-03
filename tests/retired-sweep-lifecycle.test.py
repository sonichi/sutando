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

    # --- routing is read by the marker owner: dm-only wins, redirects survive D7 + IPv6
    cases = [
        ("[dm-only]\nprivate\n", "[dm-only]\n"),
        ("private [dm-only] inline\n[channel: 1535008729106485288]\n", "[dm-only]\n"),
        ("[channel: 1535008729106485288]\nhead\n", "[channel: 1535008729106485288]\n"),
        ("**[core: 2]**\n[channel: C0AB12]\nhead\n", "[channel: C0AB12]\n"),
        ("[channel: !r:[2001:db8::1]:8448]\nhead\n", "[channel: !r:[2001:db8::1]:8448]\n"),
        ("plain head\n", ""),
    ]
    for prefix, want in cases:
        got = rd._carried_redirect(prefix.encode())
        check(f"carried routing for {prefix.splitlines()[0]!r} -> {want!r}", got == want, f"got={got!r}")
    retired = retire_with_late(d, "proactive-60.txt", "private [dm-only] inline\n[channel: 1535008729106485288]\n", "late secret\n")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("an inline dm-only prefix republishes its remainder dm-only, never as a redirect",
          len(pub) == 1 and pub[0].read_text() == "[dm-only]\nlate secret\n", f"{[x.read_text() for x in pub]}")
    for f in late_files(d):
        f.unlink()

    # --- a split multibyte append is retried whole, never delivered as replacement characters
    retired = retire_with_late(d, "proactive-61.txt", "FIRST\n", "")
    with open(retired, "ab") as fh:
        fh.write("日".encode()[:1])
    check("a torn character publishes nothing and leaves the cursor",
          rd.sweep_retired(d, quiesce_s=600, now=time.time()) == []
          and rd._delivered_marker(retired).read_text() == "6")
    with open(retired, "ab") as fh:
        fh.write("日".encode()[1:] + b"\n")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("the whole character is delivered once it is complete",
          len(pub) == 1 and pub[0].read_text() == "日\n", f"{[x.read_bytes() for x in pub]}")
    for f in late_files(d):
        f.unlink()

    # --- an old inode: a fresh lease over it is not stale, so racing sweepers publish once
    retired = retire_with_late(d, "proactive-62.txt", "FIRST\n", "LATE9\n")
    old = time.time() - 7200
    os.utime(retired, (old, old))
    held = retired.with_name(retired.name + rd._SWEEP_SUFFIX)
    first = rd._claim_for_sweep(retired, time.time(), 600)
    second = rd._claim_for_sweep(retired, time.time(), 600)
    check("old inode: a fresh lease is exclusive (second claim refused)",
          first is not None and second is None, f"first={first} second={second}")
    held.unlink(missing_ok=True)
    barrier = threading.Barrier(2)
    results = []

    def run_old():
        barrier.wait()
        results.append(rd.sweep_retired(d, quiesce_s=600, now=time.time()))

    ts = [threading.Thread(target=run_old) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("old inode: racing sweepers publish exactly one remainder",
          sum(len(r) for r in results) == 1 and len(late_files(d)) == 1, f"{results} {late_files(d)}")
    for f in late_files(d):
        f.unlink()

    # --- crash between the marker advance and publication: replayed after restart, once
    retired = retire_with_late(d, "proactive-63.txt", "FIRST\n", "LATE10\n")
    marker = rd._delivered_marker(retired)
    real_replace = os.replace

    def _crash(src, dst, *a, **k):
        if Path(dst).name.startswith("proactive-late-"):
            raise KeyboardInterrupt  # the process dies here
        return real_replace(src, dst, *a, **k)

    rd.os.replace = _crash
    try:
        try:
            rd.sweep_retired(d, quiesce_s=600, now=time.time())
        except KeyboardInterrupt:
            pass
    finally:
        rd.os.replace = real_replace
    held = retired.with_name(retired.name + rd._SWEEP_SUFFIX)
    check("crash state: marker advanced, stage on disk, nothing visible",
          marker.read_text() == "13" and not late_files(d)
          and [p for p in d.iterdir() if p.name.startswith(".proactive-63.txt.late-")],
          f"marker={marker.read_text()!r} files={sorted(x.name for x in d.iterdir())}")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time() + 601)
    check("restarted sweep after quiescence publishes the staged remainder exactly once",
          len(pub) == 1 and pub[0].read_text().strip() == "LATE10", f"{pub}")
    check("and the inode is not aged out from under it, nor published twice",
          rd.sweep_retired(d, quiesce_s=600, now=time.time() + 602) == [] and len(late_files(d)) == 1)
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


    # --- retire: the undo itself failing leaves the inode in retired/ (aged out later), still False
    claim = d / "proactive-53.txt"; claim.write_text("FIRST\n")
    real_replace = Path.replace
    def _undo_fails(self, target, *a, **k):
        if self.parent.name == "retired":
            raise OSError("undo refused")
        return real_replace(self, target, *a, **k)
    rd._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError("marker disk full"))
    Path.replace = _undo_fails
    try:
        ok = rd.retire_claim_if_unchanged(claim, "FIRST")
    finally:
        Path.replace = real_replace; rd._write_atomic = real_write
    check("marker failure + undo failure -> False, inode kept under retired/ (never destroyed)",
          ok is False and rd._retired_path(claim).exists() and not claim.exists())
    rd._retired_path(claim).unlink()

    # --- _write_atomic: a failing rename leaves no scratch behind and re-raises
    real_os_replace = rd.os.replace
    rd.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("rename refused"))
    try:
        try:
            rd._write_atomic(d / "retired" / "x.delivered", "1"); raised = False
        except OSError:
            raised = True
    finally:
        rd.os.replace = real_os_replace
    check("_write_atomic re-raises and leaves no scratch",
          raised and not list((d / "retired").glob(".x.delivered.*")) and not (d / "retired" / "x.delivered").exists())

    # --- redirect carry skips blank lines before the marker
    check("a blank line before [channel:] does not hide it",
          rd._carried_redirect(b"\n[channel: 123456789012345678]\nFIRST\n") == "[channel: 123456789012345678]\n")
    check("a plain head carries nothing", rd._carried_redirect(b"FIRST\n") == "")

    # --- sweep claim: a vanished inode or a refused lease is a skip, never a raise
    check("claim on a vanished inode -> None", rd._claim_for_sweep(d / "retired" / "gone.txt", time.time(), 600) is None)
    retired = retire_with_late(d, "proactive-46b.txt", "FIRST\n", "LATE5\n")
    real_open = os.open

    def _refuse(path, *a, **k):
        if str(path).endswith(rd._SWEEP_SUFFIX):
            raise PermissionError("claim refused")
        return real_open(path, *a, **k)

    rd.os.open = _refuse
    try:
        check("claim with a refused lease -> None",
              rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [] and not late_files(d))
    finally:
        rd.os.open = real_open
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("...and sweeps it once the lease can be taken", len(pub) == 1, f"{pub}")
    for f in late_files(d):
        f.unlink()

    # --- publication failure after the marker advanced: staged, replayed, never lost
    retired = retire_with_late(d, "proactive-49b.txt", "FIRST\n", "LATE8\n")
    marker = rd._delivered_marker(retired)
    real_replace = os.replace

    def _refuse_publish(src, dst, *a, **k):
        if Path(dst).name.startswith("proactive-late-"):
            raise OSError("publish refused")
        return real_replace(src, dst, *a, **k)

    rd.os.replace = _refuse_publish
    try:
        pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    finally:
        rd.os.replace = real_replace
    stages = [p for p in d.iterdir() if p.name.startswith(".proactive-49b.txt.late-")]
    check("publish failure -> nothing visible, marker advanced, the stage is kept as the journal",
          pub == [] and not late_files(d) and marker.read_text() == "12" and len(stages) == 1,
          f"pub={pub} marker={marker.read_text()!r} stages={stages}")
    pub = rd.sweep_retired(d, quiesce_s=600, now=time.time())
    check("the next sweep replays the stage: published once, from the journal",
          len(pub) == 1 and pub[0].read_text().strip() == "LATE8"
          and not [p for p in d.iterdir() if p.name.startswith(".proactive-49b")], f"{pub}")
    check("and not again", rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [])
    for f in late_files(d):
        f.unlink()

    # --- a whitespace-only remainder advances the marker and publishes nothing
    retired = retire_with_late(d, "proactive-56.txt", "FIRST\n", "   \n")
    marker = rd._delivered_marker(retired)
    check("whitespace remainder -> no publish, marker advanced to the full length",
          rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [] and marker.read_text() == str(retired.stat().st_size))
    with open(retired, "a") as fh: fh.write("  \n")
    rd._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError("marker disk full"))
    try:
        check("...and a marker failure there is swallowed", rd.sweep_retired(d, quiesce_s=600, now=time.time()) == [])
    finally:
        rd._write_atomic = real_write

print(f"\n{'FAILED' if FAILS else 'OK'} — {FAILS} failure(s)")
sys.exit(1 if FAILS else 0)
