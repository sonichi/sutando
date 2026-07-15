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
