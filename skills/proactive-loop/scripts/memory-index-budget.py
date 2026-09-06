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
import os
import sys
import tempfile
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


# MEMORY_DIR is derived from THIS process's cwd, not from the tree the session
# loads; a checkout can host several. Resolve by IDENTITY, never by freshness.


def _host_label(repo: Path) -> "str | None":
    """Delegate to src/util_paths._host_label; a private copy drifts from the
    per-host contract the rest of the workspace addresses."""
    try:
        spec = importlib.util.spec_from_file_location(
            "_mib_util_paths", repo / "src" / "util_paths.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        label = (mod._host_label() or "").strip()
        return label or None
    except Exception:
        return None


def _host_pointer_path(projects: Path, repo: Path) -> "Path | None":
    """Where THIS host records which corpus it loads, or None if the label owner
    cannot answer -- guessing the label is what the delegation exists to stop."""
    label = _host_label(repo)
    if not label:
        return None
    return projects.parent.parent / "hosts" / label / "memory-corpus"


def _host_stated_index(projects: Path, repo: Path) -> "tuple[Path | None, str]":
    """(index, why) from the pointer; (None, "") when unrecorded -- absent is
    absent, so the caller still refuses rather than inferring."""
    ptr = _host_pointer_path(projects, repo)
    if ptr is None:
        return None, ""
    try:
        raw = ptr.read_text().strip()
    except OSError:
        return None, ""
    if not raw:
        return None, ""
    return Path(raw).expanduser(), f"{ptr}"


def _live_index(memory_dir: Path, repo: Path) -> "tuple[Path | None, str]":
    """(index, note); index None means refuse and the note says why. Freshness is
    not identity -- an mtime cannot say which corpus this session loads."""
    default = memory_dir / "MEMORY.md"
    # MEMORY_DIR is already derived FROM this override when it is set, so the
    # caller's memory_dir is owner-stated identity -- do not re-read the var.
    if os.environ.get("SUTANDO_MEMORY_DIR"):
        if default.is_file():
            return default, ""
        return None, f"CANNOT ANSWER: SUTANDO_MEMORY_DIR resolves to {default}, which is not a file"
    projects = memory_dir.parent.parent
    stated, why = _host_stated_index(projects, repo)
    if stated is not None:
        if stated.is_file():
            return stated, ""
        return None, (f"CANNOT ANSWER: {why} names {stated}, which is not a file — "
                      f"fix that pointer rather than guessing past it.")
    try:
        cands = sorted(p for p in projects.glob("*/memory/MEMORY.md") if p.is_file())
    except OSError as e:
        # A failed scan establishes nothing about uniqueness, so falling back to
        # the cwd-derived default is the same fail-open this resolver exists to close.
        return None, (f"CANNOT ANSWER: could not scan {projects} ({e}) — uniqueness "
                      f"unestablished. Pass --index or set SUTANDO_MEMORY_DIR.")
    if not cands:
        if default.is_file():
            return default, ""
        return None, f"CANNOT ANSWER: no index at {default} and none under {projects}"
    if len(cands) == 1:
        return cands[0], ""
    names = ", ".join(c.parent.parent.name for c in cands)
    ptr = _host_pointer_path(projects, repo) or "<hosts/<label>/memory-corpus>"
    ambiguity = (
        f"AMBIGUOUS CORPUS: {len(cands)} candidate indexes under {projects} and nothing "
        f"authoritative names one ({names}). An mtime says which was edited last, not "
        f"which one this session loads.\n"
        f"RECORD IT ONCE -- this host then answers unattended forever:\n"
        f"    memory-index-budget.py --record <abs path to the MEMORY.md this session loads>\n"
        f"    (writes {ptr}, validated and atomic)\n"
        f"(or set SUTANDO_MEMORY_DIR, or pass --index for a one-off.)")
    # Refusing here makes the guard inert on every multi-corpus host, which costs
    # more than an answer carrying its own caveat. Only a MISSING default refuses.
    if default.is_file():
        return default, (ambiguity + f"\nANSWERING FROM THE DEFAULT {default} — it may not be "
                         f"the corpus this session loads; the number below is unverified.")
    return None, "CANNOT ANSWER: " + ambiguity



def record_pointer(projects: Path, repo: Path, target: Path,
                   force: bool = False) -> "tuple[int, str]":
    """THE writer for this host's corpus pointer. Validates, then writes atomically.

    A pointer naming nothing turns every later call into a refusal that reads as a
    missing corpus, so the validation is the contract, not a courtesy.
    """
    ptr = _host_pointer_path(projects, repo)
    if ptr is None:
        return 2, ("CANNOT ANSWER: this host's label is unresolvable, so there is no "
                   "pointer path to write. Set SUTANDO_HOST_LABEL or pass --index.")
    target = target.expanduser()
    if not target.is_file():
        return 2, f"REFUSED: {target} is not a file — a pointer must name an existing index."
    if target.name != "MEMORY.md":
        return 2, f"REFUSED: {target} is not named MEMORY.md — that is not an index."
    # Bootstrap only. A writer that overwrites silently can retarget a host that was
    # already correct, and this record is the one input the reader cannot sanity-check.
    if ptr.exists() and not force:
        try:
            cur = ptr.read_text(encoding="utf-8").strip()
        except OSError as e:
            cur = f"<unreadable: {e}>"
        return 2, (f"REFUSED: {ptr} already records {cur!r}. This operation bootstraps a "
                   "missing pointer; pass --force to retarget an existing one deliberately.")
    resolved = target.resolve()
    try:
        ptr.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=f".{ptr.name}.", suffix=".tmp", dir=str(ptr.parent))
    except OSError as e:
        return 2, f"REFUSED: cannot stage a write next to {ptr} ({e})."
    tmp = Path(staged)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(resolved) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ptr)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return 0, f"recorded: {ptr} -> {resolved}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]),
                    help="repo holding src/health-check.py (default: this checkout)")
    ap.add_argument("--index", help="path to MEMORY.md (default: from the repo's MEMORY_DIR)")
    ap.add_argument("--record", metavar="MEMORY_MD",
                    help="record THIS host's corpus pointer (validated, atomic) and exit")
    ap.add_argument("--force", action="store_true",
                    help="with --record: retarget a pointer that already exists")
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

    if a.record:
        rc, msg = record_pointer(Path(getattr(mod, "MEMORY_DIR", "")).parent.parent,
                                 repo, Path(a.record), force=a.force)
        print(msg, file=sys.stderr if rc else sys.stdout)
        return rc

    if a.index:
        index, note = Path(a.index), ""
    else:
        index, note = _live_index(Path(getattr(mod, "MEMORY_DIR", "")), repo)
        # A note now travels with a RESOLVED index too, carrying the caveat that
        # made it ambiguous; only a None index is a refusal.
        if note:
            print(note, file=sys.stderr)
        if index is None:
            return 2
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
        print("\n  Free room FIRST. Run scripts/memory-hub-containment.py before trimming,")
        print("  so a row you remove is still carried by its hub.")
    if r["addition_loads"] is False:
        bad = True
        print("\n✗ REFUSE — the addition itself lands past the cut and would never load.")
    if not bad:
        print("\n✓ safe — no row that loads today stops loading, and the addition loads.")
    return 1 if (bad or r["already_dropped"]) else 0


if __name__ == "__main__":
    sys.exit(main())
