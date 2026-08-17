#!/usr/bin/env python3
"""The unavailable marker must ride the near-limit WARN, and only that (#2958).

`tests/health-check-index-trend-unavailable.test.py` calls `_index_growth_note`
directly, so nothing there asserts WHERE its return value lands. That is how the
belief "the marker shows up in the memory-index detail" survived into a green
suite while the only call site sits behind `elif near_limit:` — a clean `ok`
return happens first and never reaches the helper.

This drives `check_memory_index_integrity` end to end instead, on a memory dir
with no git history (a supported single-machine configuration, where every path
in the helper yields the marker), and pins both directions: present on the
near-limit warn, absent from the clean ok.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "src" / "health-check.py"
MARK = "growth trend unavailable"

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got {got!r}, want {want!r}")
        fails.append(name)


def probe_on(memory_dir):
    """Load the module against `memory_dir` and return its memory-index result.

    MEMORY_DIR is resolved once at import, so the env has to be set before the
    module executes — and asserted after, since a resolver that ignored the
    override would otherwise report on the real host corpus.
    """
    prev = os.environ.get("SUTANDO_MEMORY_DIR")
    os.environ["SUTANDO_MEMORY_DIR"] = str(memory_dir)
    try:
        spec = importlib.util.spec_from_file_location("hc_call_site", MOD)
        m = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(m)
        except SystemExit:
            pass
        assert m.MEMORY_DIR == Path(memory_dir), (
            f"module resolved MEMORY_DIR to {m.MEMORY_DIR}, not the temp dir — "
            f"the override was ignored and this test would be reading the real corpus"
        )
        return m.check_memory_index_integrity()
    finally:
        if prev is None:
            os.environ.pop("SUTANDO_MEMORY_DIR", None)
        else:
            os.environ["SUTANDO_MEMORY_DIR"] = prev


def build(dirpath, index_lines):
    """A memory dir holding one indexed file, plus filler entries in the index.

    The filler pushes line count toward the 200-line cut without adding files:
    every real memory file stays named in the loaded prefix, so `unindexed`,
    `stranded` and `beyond_cut` are all empty and `near_limit` is the only
    thing that can select the warn branch.
    """
    d = Path(dirpath)
    (d / "a_real_memory.md").write_text("body\n")
    body = ["- [Real](a_real_memory.md) — the one file on disk"]
    body += [f"- [Filler {i}](filler_{i}.md) — index text only" for i in range(index_lines)]
    (d / "MEMORY.md").write_text("\n".join(body) + "\n")
    return d


print("check_memory_index_integrity — where the unavailable marker lands")

# 180 lines is the 0.9 * 200 near-limit trip; 200 would also truncate, which
# selects the `beyond_cut` branch instead and would not exercise this one.
with tempfile.TemporaryDirectory() as td:
    near = probe_on(build(td, 185))
    check("a near-limit index warns", (near or {}).get("status"), "warn")
    check("...and its detail carries the unavailable marker",
          MARK in (near or {}).get("detail", ""), True)

with tempfile.TemporaryDirectory() as td:
    clean = probe_on(build(td, 3))
    check("a small index is ok", (clean or {}).get("status"), "ok")
    check("...and its detail does NOT carry the marker",
          MARK in (clean or {}).get("detail", ""), False)
    # `not in` alone is satisfied by an ok branch that renders NOTHING, so the
    # clean line is pinned positively too — absence must mean absence-of-marker.
    check("...because the clean line says its own thing, not nothing",
          "all memory files reachable" in (clean or {}).get("detail", ""), True)

if fails:
    print(f"\n{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("\nAll checks passed")
