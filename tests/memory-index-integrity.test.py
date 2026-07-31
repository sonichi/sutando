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

# --- (c) the index outgrowing what a SESSION ACTUALLY READS ---------------
# Claude Code loads the first 200 lines or 25KB of a memory file, whichever
# comes first; content beyond that is dropped (the prefix still loads), and
# YAML frontmatter + block-level HTML comments are stripped BEFORE the limits
# are measured. https://code.claude.com/docs/en/memory#how-it-works
#
# These expectations are written as LITERALS from that document, deliberately
# not as `hc.MEMORY_INDEX_LOAD_*`. An earlier revision derived its assertions
# from the shipped constants, so the suite agreed with whatever the code said
# and could not detect a wrong cutoff at all (qingyun-wu, #2449).
check("shipped line limit matches the documented 200", hc.MEMORY_INDEX_LOAD_LINES == 200,
      f"got {hc.MEMORY_INDEX_LOAD_LINES}")
check("shipped byte limit matches the documented 25KB", hc.MEMORY_INDEX_LOAD_BYTES == 25 * 1024,
      f"got {hc.MEMORY_INDEX_LOAD_BYTES}")

def _mem_with(index_body: str, extra=()):
    """A memory tree whose MEMORY.md is exactly `index_body`."""
    t = tempfile.mkdtemp()
    mem = make_tree(Path(t))
    (mem / "good-memory.md").write_text("body")
    for name in extra:
        (mem / name).write_text("body")
    (mem / "MEMORY.md").write_text(index_body)
    hc.MEMORY_DIR = mem
    return mem

_ENTRY = "- [Good](good-memory.md)\n"

# john-the-dev's fixture 1: the ONLY index entry sits on line 201. The runtime
# drops that line, so the memory never loads — reading the whole file instead
# of the loaded prefix reported this as healthy.
r = hc.check_memory_index_integrity() if _mem_with(
    "".join(f"- filler {i}\n" for i in range(200)) + _ENTRY) else None
check("entry parked on line 201 → fail", r and r["status"] == "fail", str(r))
check("...and names the memory that will not load",
      r and "good-memory.md" in r["detail"], str(r))
check("...and does NOT claim the whole index stops loading",
      r and "WHOLE index" not in r["detail"] and "whole index" not in r["detail"], str(r))

# john-the-dev's fixture 2: one visible entry plus a 25KiB block-level HTML
# comment. The runtime strips the comment before measuring, so the index is
# tiny in practice — counting raw bytes reported a hard failure.
_mem_with(_ENTRY + "<!--\n" + ("padding padding padding\n" * 1100) + "-->\n")
r = hc.check_memory_index_integrity()
check("25KiB block HTML comment → not a failure", r and r["status"] != "fail", str(r))

# Same idea for YAML frontmatter.
_mem_with("---\n" + ("meta: x\n" * 900) + "---\n" + _ENTRY)
r = hc.check_memory_index_integrity()
check("large YAML frontmatter → not a failure", r and r["status"] != "fail", str(r))

# The BYTE limit binds too, not just the line limit: few lines, but the entry
# is pushed past 25KB.
_mem_with(("x" * 4000 + "\n") * 7 + _ENTRY)
r = hc.check_memory_index_integrity()
check("entry pushed past the 25KB byte limit → fail", r and r["status"] == "fail", str(r))

# ...and "whichever comes first" means the LINE limit can bind while the file
# is nowhere near 25KB.
_mem_with("".join(f"- f{i}\n" for i in range(400)) + _ENTRY)
r = hc.check_memory_index_integrity()
check("line limit binds well under 25KB → fail", r and r["status"] == "fail", str(r))

# Approaching the limit warns while everything still loads, so there is room to
# compact deliberately rather than after entries have already been dropped.
_mem_with(_ENTRY + "".join(f"- f{i}\n" for i in range(185)))
r = hc.check_memory_index_integrity()
check("near the line limit, nothing dropped yet → warn", r and r["status"] == "warn", str(r))
check("...and the warn is about approaching, not loss",
      r and "approaching" in r["detail"], str(r))

# A comfortable index stays quiet and still reports its size.
_mem_with(_ENTRY)
r = hc.check_memory_index_integrity()
check("small clean index → ok", r and r["status"] == "ok", str(r))
check("ok detail states the loadable size", r and "session read limit" in r["detail"], str(r))

# Loss and the pre-existing modes are independent.
_mem_with("".join(f"- filler {i}\n" for i in range(200)) + _ENTRY, extra=("orphan-memory.md",))
r = hc.check_memory_index_integrity()
check("dropped entry + orphan → still fail", r and r["status"] == "fail", str(r))
check("dropped entry + orphan → still names the orphan",
      r and "orphan-memory.md" in r["detail"], str(r))

# An absent MEMORY.md is not a size failure — a fresh install must stay quiet.
t = tempfile.mkdtemp(); mem = make_tree(Path(t))
(mem / "good-memory.md").write_text("body"); hc.MEMORY_DIR = mem
r = hc.check_memory_index_integrity()
check("no MEMORY.md at all → not a size failure", r and r["status"] != "fail", str(r))

# --- round-5 findings: both are "model the contract precisely" refinements ---
# The runtime strips block HTML comments, but PRESERVES them inside code fences;
# and its limit is a BYTE prefix, so a line straddling the cut is partially read.
# Getting either wrong reintroduces exactly the false verdicts this check exists
# to prevent — one in each direction.

# john-the-dev: a big comment INSIDE a fence is real content to the runtime.
_FENCED = "```html\n<!--\n" + ("padding padding padding\n" * 1200) + "-->\n```\n" + _ENTRY
_mem_with(_FENCED)
r = hc.check_memory_index_integrity()
check("28KB HTML comment inside a ```fence is NOT stripped → entry is past the cut",
      r and r["status"] != "ok", str(r))
check("...and the fenced comment still counts toward the measured size",
      len(hc._index_effective_text(_FENCED).encode()) > 25 * 1024,
      f"effective={len(hc._index_effective_text(_FENCED).encode())}")
# Discriminating control: the SAME comment unfenced must still be stripped, so
# this is fence-awareness rather than "stop stripping comments".
_UNFENCED = "<!--\n" + ("padding padding padding\n" * 1200) + "-->\n" + _ENTRY
_mem_with(_UNFENCED)
r = hc.check_memory_index_integrity()
check("the SAME comment UNfenced is still stripped → still ok", r and r["status"] == "ok", str(r))
check("...and measures small once stripped",
      len(hc._index_effective_text(_UNFENCED).encode()) < 1024,
      f"effective={len(hc._index_effective_text(_UNFENCED).encode())}")
check("~~~ fences are honoured too, not just backticks",
      len(hc._index_effective_text("~~~\n<!--\n" + ("p\n" * 500) + "-->\n~~~\n").encode()) > 500)

# rui-sutando-codex: a 4-backtick fence containing an inner ``` line. The marker
# was truncated to three characters, so the inner line closed the fence early and
# the comment after it was stripped as if unfenced — a 28KB file measured 39
# bytes and returned a false `ok`. CommonMark closes a fence only on the SAME
# character, at least as long as the opener, alone on its line.
_BIGC = "<!--\n" + ("padding padding padding\n" * 1200) + "-->\n"
for _label, _body in (
    ("4-backtick fence with an inner ``` line", "````\n```\n" + _BIGC + "````\n" + _ENTRY),
    ("4-tilde fence with an inner ~~~ line",    "~~~~\n~~~\n" + _BIGC + "~~~~\n" + _ENTRY),
    ("4-backtick fence carrying an info string", "````html\n" + _BIGC + "````\n" + _ENTRY),
):
    _mem_with(_body)
    r = hc.check_memory_index_integrity()
    check(f"{_label} → comment is NOT stripped", r and r["status"] != "ok", str(r))
# Controls: the fix must not simply stop stripping, and must not stop closing.
_mem_with("<!--\n" + ("padding padding padding\n" * 1200) + "-->\n" + _ENTRY)
check("control: an UNfenced comment is still stripped",
      (hc.check_memory_index_integrity() or {}).get("status") == "ok")
_mem_with("```\n<!--\n-->\n```\n" + _ENTRY)
check("control: a fence still CLOSES (small fenced comment stays ok)",
      (hc.check_memory_index_integrity() or {}).get("status") == "ok")

# qingyun-wu: CommonMark bounds a fence marker to at most THREE spaces of
# indentation. At four it is an indented code line, so a comment after it is NOT
# fenced and must still be stripped — treating it as a fence produced a false
# `fail` on an index that loads fine. Pinned in both directions plus the closer.
_BIGC2 = "<!--\n" + ("padding padding padding\n" * 1200) + "-->\n"
for _label, _body, _want in (
    ("4-space opener is NOT a fence (comment strips)",
     "    ```html\n" + _BIGC2 + "    ```\n" + _ENTRY, "ok"),
    ("4-space tilde opener is NOT a fence",
     "    ~~~\n" + _BIGC2 + "    ~~~\n" + _ENTRY, "ok"),
    ("3-space opener IS a fence (comment preserved)",
     "   ```html\n" + _BIGC2 + "   ```\n" + _ENTRY, "fail"),
    ("0-space opener IS a fence (regression)",
     "```html\n" + _BIGC2 + "```\n" + _ENTRY, "fail"),
    ("a 4-space CLOSER must not close a real fence",
     "```html\n" + _BIGC2 + "    ```\n" + _ENTRY, "fail"),
):
    _mem_with(_body)
    r = hc.check_memory_index_integrity()
    check(f"fence indent: {_label}", r and r["status"] == _want, f"got {r and r['status']} :: {r}")

# qingyun-wu: the byte limit cuts THROUGH a line; the filename before the cut is
# still read, so the memory loads and must not be reported lost.
_mem_with(("x" * (25 * 1024 - 100)) + "\n- [Good](good-memory.md) " + ("d" * 4000) + "\n")
r = hc.check_memory_index_integrity()
check("entry starting just BEFORE the 25KB cut is read → not a failure",
      r and r["status"] != "fail", str(r))
# Discriminating control: an entry starting AFTER the cut is genuinely lost.
_mem_with(("x" * (25 * 1024 + 50)) + "\n" + _ENTRY)
r = hc.check_memory_index_integrity()
check("entry starting AFTER the cut is still a failure", r and r["status"] == "fail", str(r))

# --- the undeclared env knobs are GONE ------------------------------------
# AGENTS.md forbids inventing undocumented env vars, and these mirrored a
# documented external contract that a deployment cannot legitimately retune
# (qingyun-wu, #2449). Asserted structurally so they cannot quietly return.
for _gone in ("MEMORY_INDEX_FAIL_BYTES", "MEMORY_INDEX_WARN_BYTES", "_positive_int_env"):
    check(f"removed: hc.{_gone}", not hasattr(hc, _gone), f"{_gone} is back")
_src = HC.read_text()
for _var in ("SUTANDO_MEMORY_INDEX_FAIL_BYTES", "SUTANDO_MEMORY_INDEX_WARN_BYTES"):
    check(f"no undeclared env var {_var}", _var not in _src)

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
