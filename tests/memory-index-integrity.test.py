#!/usr/bin/env python3
"""Test check_memory_index_integrity() — the health probe that catches memories
which exist on disk but will never load (not in MEMORY.md, or stranded in a
*-BACKUP tree). Run: python3 tests/memory-index-integrity.test.py"""
from __future__ import annotations

import importlib.util
import tempfile
import sys
from pathlib import Path

HC = Path(__file__).resolve().parent.parent / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("health_check", HC)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

_failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _failed
    print(("  ok  " if cond else "  FAIL ") + name + (("" if cond else " — " + detail)))
    if not cond:
        _failed += 1


def make_tree(tmp: Path) -> Path:
    """<tmp>/home/projects/<slug>/memory — return the memory dir."""
    mem = tmp / "home" / "projects" / "slug" / "memory"
    mem.mkdir(parents=True)
    return mem


# 1) All indexed → ok.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    (mem / "MEMORY.md").write_text("# Index\n- [Good](good-memory.md) — hook\n")
    (mem / "good-memory.md").write_text("body")
    hc.MEMORY_DIR = mem
    r = hc.check_memory_index_integrity()
    check("all-indexed → ok", r and r["status"] == "ok", str(r))

# 2) An unindexed live memory → warn naming it.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    (mem / "MEMORY.md").write_text("# Index\n- [Good](good-memory.md)\n")
    (mem / "good-memory.md").write_text("body")
    (mem / "orphan-memory.md").write_text("stranded rule that never loads")
    hc.MEMORY_DIR = mem
    r = hc.check_memory_index_integrity()
    check("unindexed live memory → warn", r and r["status"] == "warn", str(r))
    check("warn names the orphan file", r and "orphan-memory.md" in r["detail"], str(r))

# 3) A memory stranded in a sibling *-BACKUP tree, absent from live → warn.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    (mem / "MEMORY.md").write_text("# Index\n")
    backup_mem = Path(t) / "home-BACKUP-20260714" / "projects" / "slug" / "memory"
    backup_mem.mkdir(parents=True)
    (backup_mem / "gmail-imap-capability.md").write_text("the IMAP technique")
    (backup_mem / "MEMORY.md").write_text("# Index\n")
    hc.MEMORY_DIR = mem
    r = hc.check_memory_index_integrity()
    check("backup-stranded memory → warn", r and r["status"] == "warn", str(r))
    check("warn names the stranded file", r and "gmail-imap-capability.md" in r["detail"], str(r))

# --- (c) the index itself outgrowing the session read limit --------------
# Modes (a) and (b) each lose ONE memory. This one loses the whole index at
# once, silently, while every memory file on disk still looks healthy — so it
# is tested at the boundary rather than with a single comfortable value.
# Thresholds are pinned to explicit test values so the assertions do not drift
# when the shipped defaults are retuned.
_FAIL = hc.MEMORY_INDEX_FAIL_BYTES
_WARN = hc.MEMORY_INDEX_WARN_BYTES
check("shipped defaults leave real headroom (warn strictly below fail)", _WARN < _FAIL,
      f"warn={_WARN} fail={_FAIL}")

def _index_of(mem: Path, nbytes: int) -> None:
    """Write a MEMORY.md of exactly nbytes that indexes one real memory."""
    head = "# Index\n- [Good](good-memory.md) — "
    (mem / "good-memory.md").write_text("body")
    pad = "x" * max(0, nbytes - len(head.encode()) - 1)
    (mem / "MEMORY.md").write_text(head + pad + "\n")

for label, size, want in (
    ("well under warn        → ok",   _WARN - 2048, "ok"),
    ("one byte under warn    → ok",   _WARN - 1,    "ok"),
    ("exactly at warn        → warn", _WARN,        "warn"),
    ("between warn and fail  → warn", (_WARN + _FAIL) // 2, "warn"),
    ("one byte under fail    → warn", _FAIL - 1,    "warn"),
    ("exactly at fail        → fail", _FAIL,        "fail"),
    ("far over fail          → fail", _FAIL + 8192, "fail"),
):
    with tempfile.TemporaryDirectory() as t:
        mem = make_tree(Path(t))
        _index_of(mem, size)
        hc.MEMORY_DIR = mem
        r = hc.check_memory_index_integrity()
        check(f"index size: {label}", r and r["status"] == want,
              f"size={size} got={r and r['status']} want={want} :: {r and r['detail'][:90]}")

# The ok path should still report the size, so the number is visible BEFORE it
# becomes a problem — a threshold you only see once it trips is a threshold you
# cannot plan around.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    _index_of(mem, 1024)
    hc.MEMORY_DIR = mem
    r = hc.check_memory_index_integrity()
    check("ok detail states the index size", r and "KB index" in r["detail"], str(r))

# Size and the pre-existing modes are independent: an oversized index must
# still name the orphan, and must not be downgraded to warn by its presence.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    _index_of(mem, _FAIL + 512)
    (mem / "orphan-memory.md").write_text("stranded rule that never loads")
    hc.MEMORY_DIR = mem
    r = hc.check_memory_index_integrity()
    check("oversized + orphan → still fail", r and r["status"] == "fail", str(r))
    check("oversized + orphan → still names the orphan",
          r and "orphan-memory.md" in r["detail"], str(r))

# An absent MEMORY.md is 0 bytes, not "oversized" — a fresh install must not
# trip the size guard.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    (mem / "good-memory.md").write_text("body")
    hc.MEMORY_DIR = mem
    r = hc.check_memory_index_integrity()
    check("no MEMORY.md at all → not a size failure", r and r["status"] != "fail", str(r))

# 4) Missing memory dir → None (no false alarm on fresh installs).
with tempfile.TemporaryDirectory() as t:
    hc.MEMORY_DIR = Path(t) / "does-not-exist" / "memory"
    check("missing dir → None", hc.check_memory_index_integrity() is None)

# 5) The probe is wired into run_all_checks() — a memory-index entry appears.
with tempfile.TemporaryDirectory() as t:
    mem = make_tree(Path(t))
    (mem / "MEMORY.md").write_text("# Index\n- [Orphan check](orphan.md)\n")
    (mem / "orphan.md").write_text("x")
    hc.MEMORY_DIR = mem
    names = [c.get("name") for c in hc.run_all_checks()]
    check("run_all_checks() includes memory-index", "memory-index" in names, str(names))

print()
if _failed:
    print(f"FAIL — {_failed} check(s) failed"); sys.exit(1)
print("PASS — memory-index-integrity tests")
