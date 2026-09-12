#!/usr/bin/env python3
"""iter_result_candidates must cover the UNION of the three legacy layouts.

Each legacy enumerator knew a different subset, so a result was findable or
not depending on which was asked. One case per layout, plus the ordering the
readiness resolver depends on.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "ltp", Path(__file__).resolve().parent.parent / "src" / "local_task_protocol.py")
ltp = importlib.util.module_from_spec(spec)
# register before exec: @dataclass resolves the owning module via sys.modules
sys.modules["ltp"] = ltp
spec.loader.exec_module(ltp)

fails = []


def check(name, got, want):
    if got != want:
        fails.append(name)
        print(f"  FAIL: {name}\n        got  {got}\n        want {want}")
    else:
        print(f"  OK: {name}")


TID = "task-1787760000000"
F = f"{TID}.txt"

with tempfile.TemporaryDirectory() as td:
    r = Path(td) / "results"
    for d in (r, r / "archive", r / "archive" / "2026-08", r / "archive" / "2026-07",
              r / "archive" / "quarantine", r / "archive-legacy"):
        d.mkdir(parents=True, exist_ok=True)

    cands = ltp.iter_result_candidates(r, TID)
    rel = [str(p.relative_to(r)) for p in cands]

    # --- one case per layout: each must be REACHABLE ---
    check("live", f"{F}" in rel, True)
    check("direct archive/<id>", f"archive/{F}" in rel, True)
    check("month dir", f"archive/2026-08/{F}" in rel, True)
    check("older month dir", f"archive/2026-07/{F}" in rel, True)
    check("non-month archive subdir", f"archive/quarantine/{F}" in rel, True)
    check("sibling archive-* dir", f"archive-legacy/{F}" in rel, True)

    # flat form only appears when a matching file exists
    check("flat absent when no file", any("-" in Path(p).name.replace(TID, "") and
                                          Path(p).name != F for p in rel), False)
    (r / "archive" / f"{TID}-1787760001.txt").write_text("x")
    (r / "archive" / f"{TID}-1787760009.txt").write_text("x")
    rel2 = [str(p.relative_to(r)) for p in ltp.iter_result_candidates(r, TID)]
    check("flat form present", f"archive/{TID}-1787760009.txt" in rel2, True)
    check("flat picks newest", f"archive/{TID}-1787760001.txt" in rel2, False)

    # --- ordering the readiness resolver depends on ---
    check("live is first", rel2[0], F)
    check("direct before months", rel2.index(f"archive/{F}") < rel2.index(f"archive/2026-08/{F}"), True)
    check("newer month before older",
          rel2.index(f"archive/2026-08/{F}") < rel2.index(f"archive/2026-07/{F}"), True)
    check("flat is last", rel2[-1], f"archive/{TID}-1787760009.txt")

    # --- traversal gate ---
    check("malformed id rejected", ltp.iter_result_candidates(r, "../../etc/passwd"), [])

    # --- CONTROL: the enumeration must be able to MISS something ---
    check("control: unrelated id yields no shared paths",
          set(str(p) for p in ltp.iter_result_candidates(r, "task-1787760000001"))
          & set(str(p) for p in cands), set())

print()
print(f"{len(fails)} failure(s)" if fails else "all checks passed")
sys.exit(1 if fails else 0)
