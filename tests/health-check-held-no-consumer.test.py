#!/usr/bin/env python3
"""`results/held-no-consumer/` must be watched, and its exclusion must be a prefix test.

The sibling probes glob `results/` and `results/.outbox*`; this directory matches
neither, so all three report ok while addressed results sit undelivered. The
exclusion is a substring test because disposition stamps carry dates — an
exact-match on `superseded` rescans `.superseded-2026-08-26-...` as a live entry
and reports every one of them as held.
"""
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

OLD = time.time() - 10 * 86400
FAILURES = []


def _probe(build):
    root = Path(tempfile.mkdtemp())
    (root / "results").mkdir()
    build(root / "results")
    hc.WORKSPACE_DIR = root
    return hc.check_held_no_consumer()


def _touch(path, when=OLD):
    path.write_text("x")
    os.utime(path, (when, when))


def check(label, result, want_status, want_fragments=()):
    if result["status"] != want_status:
        FAILURES.append(f"{label}: status {result['status']!r} != {want_status!r}")
        return
    for fragment in want_fragments:
        if fragment not in result["detail"]:
            FAILURES.append(f"{label}: {fragment!r} missing from {result['detail']!r}")


def _empty(results):
    (results / "held-no-consumer").mkdir()


def _only_disposed(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "proactive-1.withdrawn-wrong-transport.txt")


def _dated_disposition(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "reply.superseded-2026-08-26-by-full-reply.txt")


def _live_shape(results):
    held = results / "held-no-consumer"
    held.mkdir()
    for part in (1, 2, 3):
        _touch(held / f"pr-digest-part{part}.no-push-transport.txt")
    _touch(held / "proactive-9.withdrawn-wrong-transport.txt")


def _fresh(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "just-parked.no-push-transport.txt", time.time() - 60)


# A directory that no writer has created yet is not a finding.
check("absent", _probe(lambda results: None), "ok", ("not created",))
check("empty", _probe(_empty), "ok", ("no undisposed",))

# A disposition suffix records a decision; it is excluded, and SAID to be.
check("only-disposed", _probe(_only_disposed), "ok", ("1 carry a disposition suffix",))
check("dated-disposition", _probe(_dated_disposition), "ok", ("1 carry a disposition suffix",))

# The shape measured live: three held parts beside one withdrawn file.
check("live-shape", _probe(_live_shape), "warn",
      ("3 result(s) parked", "1 excluded by disposition suffix", "never delivered"))

# Something parked seconds ago is in flight, not stranded.
check("under-threshold", _probe(_fresh), "ok", ("no undisposed",))

# A file the probe cannot stat is partial coverage, never a clean pass. This is
# the branch a later simplification is most likely to fold into `ok`.
def _unreadable(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "parked.no-push-transport.txt")
    # Readable but not executable: iterdir lists the name, stat on the child
    # raises EACCES. Root ignores mode bits, so skip rather than assert there.
    held.chmod(0o400)


if os.geteuid() != 0:
    _root = Path(tempfile.mkdtemp())
    (_root / "results").mkdir()
    _unreadable(_root / "results")
    hc.WORKSPACE_DIR = _root
    _res = hc.check_held_no_consumer()
    (_root / "results" / "held-no-consumer").chmod(0o700)
    check("unreadable", _res, "warn", ("coverage is partial, not clean",))
else:
    print("note: running as root — unreadable arm skipped, mode bits do not apply")

if FAILURES:
    print("FAIL")
    for line in FAILURES:
        print(" ", line)
    sys.exit(1)
print("PASS: 7 controls, both directions")
