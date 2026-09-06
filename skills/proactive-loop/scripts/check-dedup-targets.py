#!/usr/bin/env python3
"""Refuse a `[deduped: X]` whose target delivers nothing.

`[deduped: X]` means "the full reply is in X's result". If X's result is
`[no-send]` (or absent), the consolidation resolves to nothing — and the failure
is only visible AFTER the bridge has already told the room so, naming an internal
task id the peer cannot resolve. Measured 2026-09-01: three collaborator notices
closed that way produced a DELIVERED outbox item reading "This was folded into
`task-<internal id>`, which delivered nothing", and the peer spent a 519-task
sweep hunting an id that never existed on their side.

Usage:
  check-dedup-targets.py                 # audit results/ + archive
  check-dedup-targets.py <file>...       # check specific result files first
Exit 0 clean, 1 contradictions found, 2 could not answer.
"""

import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def _owner():
    """`src/dedup_soundness.py`, the judgement's owner, or None if unavailable.

    Resolved through the CURRENT `REPO` rather than captured at import, so the
    absent-owner path stays reachable (and testable) instead of turning into an
    import-time crash. That distinction matters to the caller: a tool that dies
    on import exits 1, and every checker in the proactive loop reserves 1 for a
    REAL finding — so a missing owner would read as "dedups resolve to nothing"
    rather than "the checker could not answer".
    """
    src = REPO / "src"
    if not (src / "dedup_soundness.py").is_file() or not (src / "result_markers.py").is_file():
        return None
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import dedup_soundness
    return dedup_soundness


def _require_owner():
    ds = _owner()
    if ds is None:
        raise RuntimeError("src/dedup_soundness.py (or result_markers.py) not importable — "
                           "cannot answer; refusing to fall back to a weaker local rule")
    return ds


def workspace() -> Path:
    sys.path.insert(0, str(REPO / "src"))
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def check(ws: Path, files) -> "list[tuple[str, str, str]]":
    """(file, target, why) for every dedup that resolves to nothing.

    The task id is the result FILENAME's stem, which is what lets the shared
    judgement compare this task's sender and room against the holder's. Passing
    the body alone (as this file used to) makes those two checks unreachable.
    """
    ds = _require_owner()
    src = REPO / "src"
    results, tasks = ws / "results", ws / "tasks"
    bad = []
    for f in files:
        try:
            body = f.read_text(errors="ignore")
        except OSError:
            continue
        target = ds.dedup_target(body, src)
        if not target:
            continue
        why = ds.dedup_problem(
            results, _task_id(f), tasks if tasks.is_dir() else None, text=body, src_dir=src)
        if why:
            bad.append((f.name, target, why))
    return bad


def _task_id(result_file: Path) -> str:
    """`task-<id>` from a result filename, across the shapes it can carry:
    `task-x.txt`, `<channel-key>.task-x.txt`, `task-x.txt.sending`."""
    name = result_file.name
    for suffix in (".txt.sending", ".txt"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.rsplit(".", 1)[-1]


def main(argv) -> int:
    if _owner() is None:
        print("cannot answer: src/dedup_soundness.py (or result_markers.py) is not importable — "
              "refusing to fall back to a weaker local rule", file=sys.stderr)
        return 2
    ws = workspace()
    if argv:
        files = [Path(a) for a in argv]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            print(f"cannot answer: no such file(s): {missing}", file=sys.stderr)
            return 2
    else:
        rd = ws / "results"
        if not rd.is_dir():
            print("cannot answer: no results/ directory", file=sys.stderr)
            return 2
        # Results archive into month subdirectories (see agent-api.py, which iterates
        # archive/*/), so a flat glob audits almost nothing.
        files = [f for f in rd.glob("*.txt")] + [f for f in (rd / "archive").glob("**/*.txt")]
    bad = check(ws, files)
    print(f"checked {len(files)} result file(s)")
    for name, tid, why in bad:
        print(f"  CONTRADICTION {name} -> [deduped: {tid}]: {why}", file=sys.stderr)
    if bad:
        print(f"{len(bad)} dedup(s) resolve to nothing — write a real reply, or use "
              f"[no-send] on BOTH", file=sys.stderr)
        return 1
    print("no dedup resolves to a non-delivering target")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
