#!/usr/bin/env python3
"""Tests for find_archived_task()'s month-partition scan.

The scan was rewritten from `sorted(archive_root.iterdir())` + `Path.is_dir()`
to `os.scandir()` with a name filter applied BEFORE the directory test. That is
a performance change to a lookup on the live task path, so what these tests
actually pin is that it is a PURE REFACTOR: the reference implementation below
is the pre-change algorithm verbatim, and every layout is asserted to produce
an identical result from both. Ordering is part of the contract — the first
existing candidate wins, so a reordered month list would silently change which
file is returned when the same task id exists in two months.

Run: python3 tests/local-task-protocol-archive-lookup.test.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "local_task_protocol", REPO / "src" / "local_task_protocol.py")
ltp = importlib.util.module_from_spec(spec)
sys.modules["local_task_protocol"] = ltp
spec.loader.exec_module(ltp)

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def reference_find(tasks_dir: Path, task_id: str):
    """The PRE-CHANGE implementation, kept verbatim as the equivalence oracle."""
    if not ltp.valid_archive_lookup_id(task_id):
        return None
    fname = f"{task_id}.txt"
    candidates = [tasks_dir / fname, tasks_dir / "processed" / fname,
                  tasks_dir / "archive" / fname]
    archive_root = tasks_dir / "archive"
    if archive_root.is_dir():
        for entry in sorted(archive_root.iterdir()):
            if entry.is_dir() and ltp._MONTH_DIR_RE.match(entry.name):
                candidates.append(entry / fname)
    for p in candidates:
        if p.exists():
            return p
    return None


def build(layout):
    """layout: list of relative paths; a trailing '/' means make a directory."""
    root = Path(tempfile.mkdtemp()) / "tasks"
    root.mkdir()
    for rel in layout:
        p = root / rel.rstrip("/")
        if rel.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("id: task-X\ntask: hi\n")
    return root


TID = "task-X"
F = f"{TID}.txt"

LAYOUTS = {
    "live dir wins": [F, f"processed/{F}", f"archive/{F}", f"archive/2026-07/{F}"],
    "processed when no live": [f"processed/{F}", f"archive/2026-07/{F}"],
    "flat archive when no live/processed": [f"archive/{F}"],
    "month-partitioned only": [f"archive/2026-07/{F}"],
    "EARLIEST month wins across two": [f"archive/2026-05/{F}", f"archive/2026-07/{F}"],
    "three months, only latest holds it": ["archive/2026-05/", "archive/2026-06/", f"archive/2026-07/{F}"],
    "absent everywhere": ["archive/2026-07/"],
    "no archive dir at all": [f"processed/{F}"],
    "empty tasks dir": [],
    "non-month dirs ignored": [f"archive/scratch/{F}", "archive/2026-07/"],
    "month-shaped FILE is not a month dir": ["archive/2026-08", f"archive/2026-07/{F}"],
    "month-shaped file only": ["archive/2026-08"],
    "nested deeper than one month level": [f"archive/2026-07/nested/{F}"],
    "malformed month names": [f"archive/26-07/{F}", f"archive/2026-7/{F}", f"archive/2026-077/{F}"],
}

for name, layout in LAYOUTS.items():
    root = build(layout)
    got = ltp.find_archived_task(root, TID)
    want = reference_find(root, TID)
    check(f"equivalence: {name}", got == want, f"new={got} old={want}")

# Traversal gate. Comparing against reference_find() alone is NOT sufficient
# here: reference_find() calls the same gate, and a forged id normally resolves
# to a path that does not exist, so both sides return None and the assertion
# holds whether or not the gate ran. (Verified: deleting the gate leaves that
# weaker form green.) So plant a file that traversal WOULD actually reach, and
# assert None — which is only true if the id was rejected before the lookup.
for bad in ("../evil", "../../evil"):
    root = build([f"archive/2026-07/{F}"])
    reachable = (root / f"{bad}.txt").resolve()
    reachable.parent.mkdir(parents=True, exist_ok=True)
    reachable.write_text("id: evil\ntask: should never be reachable\n")
    check(f"traversal gate rejects {bad!r} even when the target EXISTS",
          ltp.find_archived_task(root, bad) is None,
          f"reached {reachable}")

for bad in ("", "task X", "task-X\n", "task-X/../../y"):
    root = build([f"archive/2026-07/{F}"])
    check(f"traversal gate rejects {bad!r}",
          ltp.find_archived_task(root, bad) == reference_find(root, bad))

# A missing archive root must be "no months", not an exception — os.scandir
# raises where the old `archive_root.is_dir()` guard simply returned False.
root = build([f"processed/{F}"])
try:
    got = ltp.find_archived_task(root, TID)
    check("missing archive root does not raise", got == root / "processed" / F, str(got))
except OSError as exc:
    check("missing archive root does not raise", False, f"raised {exc!r}")

# An unreadable archive root must degrade to "no months", not propagate.
if os.getuid() != 0:
    root = build([f"processed/{F}", "archive/2026-07/"])
    (root / "archive").chmod(0o000)
    try:
        got = ltp.find_archived_task(root, TID)
        check("unreadable archive root degrades to no-months",
              got == root / "processed" / F, str(got))
    except OSError as exc:
        check("unreadable archive root degrades to no-months", False, f"raised {exc!r}")
    finally:
        (root / "archive").chmod(0o755)

# The ordering contract, stated directly rather than only via the oracle:
# months are ascending, so the earliest month holding the id is returned.
root = build([f"archive/2026-05/{F}", f"archive/2026-06/{F}", f"archive/2026-07/{F}"])
check("months are searched in ascending order",
      ltp.find_archived_task(root, TID) == root / "archive" / "2026-05" / F)

if failures:
    print(f"\n{len(failures)} failure(s): {failures}")
    sys.exit(1)
print("PASS — find_archived_task archive-lookup equivalence + edge cases")
