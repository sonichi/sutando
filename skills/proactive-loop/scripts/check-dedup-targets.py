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

import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def _canonical():
    """`result_markers.dedup_holder_delivered`, the repo's policy owner.

    Re-implementing "did the holder deliver" drifts: a hand-rolled `[no-send]`
    test called a `[REPLIED]` holder delivered, while the owner counts EVERY skip
    action. Import it rather than copy it; if it cannot be imported the checker
    must refuse (exit 2), never fall back to a weaker local rule.
    """
    src = REPO / "src"
    if not (src / "result_markers.py").is_file():
        return None
    sys.path.insert(0, str(src))
    import result_markers
    return result_markers.dedup_holder_delivered

DEDUP = re.compile(r"^\s*\[deduped:\s*([^\]]+?)\s*\]", re.M)


def workspace() -> Path:
    sys.path.insert(0, str(REPO / "src"))
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def target_body(ws: Path, tid: str) -> "str | None":
    """The referenced result's body, or None if no such result exists."""
    tid = tid.strip()
    direct = ws / "results" / f"{tid}.txt"
    if direct.is_file():
        return direct.read_text(errors="ignore")
    hits = sorted((ws / "results" / "archive").glob(f"{tid}*.txt"))
    if hits:
        return hits[-1].read_text(errors="ignore")
    return None


def check(ws: Path, files) -> "list[tuple[str, str, str]]":
    """(file, target, why) for every dedup that resolves to nothing."""
    bad = []
    for f in files:
        try:
            body = f.read_text(errors="ignore")
        except OSError:
            continue
        m = DEDUP.search(body)
        if not m:
            continue
        why = resolve(ws, m.group(1))
        if why:
            bad.append((f.name, m.group(1), why))
    return bad


def resolve(ws: Path, tid: str) -> "str | None":
    """None if the target delivered a real reply; else why it delivers nothing.

    Delegates the whole judgement to the repo's owner, `result_markers.
    dedup_holder_delivered`. That is deliberate and it is the second time this
    file has been corrected toward it:

      - a hand-rolled `[no-send]` test called a `[REPLIED]` holder delivered;
      - hand-rolled CHAIN FOLLOWING (a -> b -> real reply => clean) was MORE
        PERMISSIVE than production. `[deduped:]` is itself a skip action, so the
        bridge's `dedup_decision` returns "requeue" for a chained holder and never
        walks it. A guard that clears what the bridge rejects is worse than none.

    So: no local rule, no recursion. Ask the owner.
    """
    tb = target_body(ws, tid)
    if tb is None:
        return "target result does not exist"
    delivered = _canonical()
    if delivered is None:
        raise RuntimeError("result_markers.py not importable — cannot answer")
    if not delivered(tb):
        nxt = DEDUP.search(tb)
        if nxt:
            return (f"target is itself [deduped: {nxt.group(1)}] — the bridge treats a chained "
                    f"holder as not delivered and requeues; it does not walk the chain")
        # Quote the target's own first line for diagnostics. The VERDICT stays
        # the owner's; this only says what the reader would see there.
        first = (tb.strip().splitlines() or [""])[0][:40]
        return f"target delivered nothing (canonical dedup_holder_delivered); it begins {first!r}"
    return None

def main(argv) -> int:
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
        files = [f for f in rd.glob("*.txt")] + [f for f in (rd / "archive").glob("*.txt")]
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
