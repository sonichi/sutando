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
from unittest import mock

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


_root = Path(tempfile.mkdtemp())
(_root / "results").mkdir()
_unreadable(_root / "results")
hc.WORKSPACE_DIR = _root
_blocked = _root / "results" / "held-no-consumer" / "parked.no-push-transport.txt"
_real_stat = Path.stat


def _stat_unless_blocked(path, *args, **kwargs):
    if path == _blocked:
        raise PermissionError("denied")
    return _real_stat(path, *args, **kwargs)


with mock.patch.object(Path, "stat", autospec=True, side_effect=_stat_unless_blocked):
    _res = hc.check_held_no_consumer()
check("unreadable", _res, "warn", ("coverage is partial, not clean",))

# With days in the tuple these three all score 0, so each prints "(0d)" and the
# "oldest" is whatever iterdir yielded first.
def _sub_day(results):
    held = results / "held-no-consumer"
    held.mkdir()
    now = time.time()
    _touch(held / "just-over.no-push-transport.txt", now - 3601)
    _touch(held / "middle.no-push-transport.txt", now - 40000)
    _touch(held / "nearly-a-day.no-push-transport.txt", now - 86399)


_sub = _probe(_sub_day)
check("sub-day-render", _sub, "warn", ("oldest 23h", "just-over.no-push-transport.txt (1h)"))
if "(0d)" in _sub["detail"]:
    FAILURES.append(f"sub-day-render: days-granularity leaked into {_sub['detail']!r}")
if _sub["detail"].index("nearly-a-day") > _sub["detail"].index("just-over"):
    FAILURES.append("sub-day-render: ordered by filesystem, not by age")


# qingyun-wu's four uncovered branches (diff coverage 90.2% at e06b34d2), each a fixture that
# reaches exactly one line; the mixed unreadable+held case is the one no earlier arm could produce.
def _unlistable(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "parked.no-push-transport.txt")


_r2 = Path(tempfile.mkdtemp())
(_r2 / "results").mkdir()
_unlistable(_r2 / "results")
hc.WORKSPACE_DIR = _r2
with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
    _res2 = hc.check_held_no_consumer()
check("unlistable-dir", _res2, "warn", ("could not scan results/held-no-consumer/",))


def _nested_dir(results):
    held = results / "held-no-consumer"
    held.mkdir()
    (held / "nested").mkdir()                       # not S_ISREG -> skipped, never counted
    _touch(held / "parked.no-push-transport.txt")


_nested = _probe(_nested_dir)
check("nested-dir-skipped", _nested, "warn", ("parked.no-push-transport.txt",))
if "nested" in _nested["detail"]:
    FAILURES.append(f"nested-dir-skipped: a directory was counted as a held result: {_nested['detail']!r}")


def _mixed(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "parked.no-push-transport.txt")
    (held / "gone.no-push-transport.txt").symlink_to(held / "missing")   # stat() raises on the link only


check("unreadable-beside-held", _probe(_mixed), "warn",
      ("parked.no-push-transport.txt", "1 unreadable, so this count is a floor"))


def _minutes(results):
    held = results / "held-no-consumer"
    held.mkdir()
    _touch(held / "young.no-push-transport.txt", time.time() - 300)


_r3 = Path(tempfile.mkdtemp())
(_r3 / "results").mkdir()
_minutes(_r3 / "results")
hc.WORKSPACE_DIR = _r3
check("minutes-render-at-lowered-threshold", hc.check_held_no_consumer(threshold_age_sec=60),
      "warn", ("young.no-push-transport.txt (5m)",))

if FAILURES:
    print("FAIL")
    for line in FAILURES:
        print(" ", line)
    sys.exit(1)
print("PASS: 12 controls, both directions")
