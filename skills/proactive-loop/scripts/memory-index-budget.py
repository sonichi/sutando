#!/usr/bin/env python3
"""Refuse a MEMORY.md addition that would silently drop an entry already loading.

WHY THIS EXISTS: `health-check.py`'s memory-index probe measures the index AFTER
the fact, so the first thing that tells you a lesson stopped loading is a warn on
a later pass — by which point the write has landed and nothing names the casualty.
The session reads a BYTE PREFIX; entries past the cut vanish while every memory
file still looks fine on disk.

THE PREDICATE IS NOT HEADROOM. "Bytes remaining" reads as a budget you may spend,
and it cannot name what spending it costs. This asks the question the harm is
actually made of: which lines load now, which load after, and what is in the
difference.

DELEGATES the measurement to health-check.py (`_index_effective_text` +
`_index_loaded_prefix` + `MEMORY_INDEX_LOAD_BYTES`). It does NOT re-derive the
25,000 and does NOT re-implement comment/fence stripping: a guard that measures
differently from the probe it guards clears writes the probe will later condemn.
Unimportable -> exit 2 (cannot answer), never a hardcoded fallback.

  (default)         report what loads now, and name anything ALREADY dropped
  --adding TEXT     would appending TEXT drop an entry, or fail to load itself?
  --adding-file F   same, reading the addition from a file
  --at-top          insert at the top instead of appending (worst case)

exit 0 safe · 1 an entry would drop (or the addition would not load) · 2 cannot answer
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _health_check(repo: Path):
    """Import health-check.py for its measurement primitives, or return None."""
    hc = repo / "src" / "health-check.py"
    if not hc.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_hc_budget", hc)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass                      # it is also a CLI; argv parsing may exit
    except Exception:
        return None
    need = ("_index_effective_text", "_index_loaded_prefix", "MEMORY_INDEX_LOAD_BYTES")
    return mod if all(hasattr(mod, n) for n in need) else None


def loaded_lines(mod, text: str) -> "tuple[list[str], int, int]":
    """The lines the session actually sees, measured exactly as the probe does."""
    effective = mod._index_effective_text(text)
    loaded, nbytes, nlines = mod._index_loaded_prefix(effective)
    return loaded.splitlines(), nbytes, nlines


def entries(lines: "list[str]") -> "set[str]":
    """Index rows, keyed by their own text — a row is the unit that is lost."""
    return {ln.strip() for ln in lines if ln.strip().startswith("-")}


def evaluate(mod, current: str, addition: str = "", at_top: bool = False) -> dict:
    before, b_bytes, b_lines = loaded_lines(mod, current)
    all_before = entries(mod._index_effective_text(current).splitlines())
    already = all_before - entries(before)

    if not addition:
        return {"mode": "report", "bytes": b_bytes, "lines": b_lines,
                "limit": mod.MEMORY_INDEX_LOAD_BYTES, "already_dropped": sorted(already),
                "dropped": [], "addition_loads": None}

    if not addition.endswith("\n"):
        addition += "\n"
    after_text = addition + current if at_top else current + addition
    after, a_bytes, a_lines = loaded_lines(mod, after_text)

    dropped = sorted(entries(before) - entries(after))
    add_rows = entries(addition.splitlines())
    addition_loads = add_rows.issubset(entries(after)) if add_rows else None
    return {"mode": "adding", "bytes": a_bytes, "lines": a_lines,
            "limit": mod.MEMORY_INDEX_LOAD_BYTES, "already_dropped": sorted(already),
            "dropped": dropped, "addition_loads": addition_loads,
            "delta_bytes": a_bytes - b_bytes}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]),
                    help="repo holding src/health-check.py (default: this checkout)")
    ap.add_argument("--index", help="path to MEMORY.md (default: from the repo's MEMORY_DIR)")
    ap.add_argument("--adding")
    ap.add_argument("--adding-file")
    ap.add_argument("--at-top", action="store_true")
    a = ap.parse_args(argv)

    repo = Path(a.repo).resolve()
    mod = _health_check(repo)
    if mod is None:
        print("CANNOT ANSWER: src/health-check.py not importable — refusing rather "
              "than measuring with a private copy of the limit", file=sys.stderr)
        return 2

    index = Path(a.index) if a.index else Path(getattr(mod, "MEMORY_DIR", "")) / "MEMORY.md"
    if not index.is_file():
        print(f"CANNOT ANSWER: no index at {index}", file=sys.stderr)
        return 2

    addition = ""
    if a.adding_file:
        addition = Path(a.adding_file).read_text()
    elif a.adding:
        addition = a.adding

    r = evaluate(mod, index.read_text(errors="ignore"), addition, a.at_top)
    head = f"{r['bytes']:,} B / {r['lines']} lines load (limit {r['limit']:,} B)"
    if r["mode"] == "adding":
        head += f"   addition: {r['delta_bytes']:+,} B"
    print(head)

    if r["already_dropped"]:
        print(f"\n⚠ ALREADY NOT LOADING — {len(r['already_dropped'])} row(s) past the cut today:")
        for e in r["already_dropped"][:10]:
            print(f"    {e[:100]}")

    if r["mode"] == "report":
        return 1 if r["already_dropped"] else 0

    bad = False
    if r["dropped"]:
        bad = True
        print(f"\n✗ REFUSE — this addition drops {len(r['dropped'])} row(s) that load today:")
        for e in r["dropped"][:10]:
            print(f"    {e[:100]}")
        # Names no containment script: the one previously cited never existed,
        # so the advice was unrunnable at the one moment it is read — a refusal.
        print("\n  Free room FIRST, and check the row is still reachable from its hub")
        print("  before removing it. health-check.py's memory-index probe reports the")
        print("  loaded prefix; which rows go is the owner's call.")
    if r["addition_loads"] is False:
        bad = True
        print("\n✗ REFUSE — the addition itself lands past the cut and would never load.")
    if not bad:
        print("\n✓ safe — no row that loads today stops loading, and the addition loads.")
    return 1 if (bad or r["already_dropped"]) else 0


if __name__ == "__main__":
    sys.exit(main())
