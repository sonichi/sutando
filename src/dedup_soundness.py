#!/usr/bin/env python3
"""Is a `[deduped: X]` sound? One owner, because the copies were not equal.

`[deduped: X]` asserts "the full reply is in X's result". Deciding whether that
holds needs three things, and two consumers had grown their own version of each:
`scripts/unanswered-tasks.py` (post-hoc, end of a pass) and
`skills/proactive-loop/scripts/check-dedup-targets.py` (the guard run BEFORE the
write). The pre-write copy was weaker in every one of them, which is the worst
direction: it cleared writes the post-hoc check would later condemn.

  1. WHICH FILE IS THE TARGET'S RESULT. A bare `{id}*` glob also matches
     `{id}.too-old.<epoch>` — a QUARANTINED result, the exact opposite of a
     delivered one. The guard used that glob, so a dedup pointing at a reply that
     was never delivered read as clean.
  2. WHETHER THE HOLDER DELIVERED. Owned by `result_markers`, not re-implemented.
  3. WHETHER THE HOLDER ANSWERS THE SAME PERSON, IN THE SAME ROOM. A holder that
     delivers to a different room leaves the asking room silent, and nothing in
     the marker grammar can see that.

Dependency-light on purpose: paths come from the caller, so this imports no
workspace resolver and no CLI. `result_markers` is the one import, and an
unavailable one raises rather than degrading to a local rule — a guard that
clears what the bridge rejects is worse than no guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MARKERS = None


def markers(src_dir: Path | None = None):
    """`(dedup_holder_delivered, parse_markers)` from the repo's policy owner.

    Raises ImportError rather than falling back: re-implementing the grammar is
    how this drifted twice (a `[REPLIED]` holder read as delivered; chain-walking
    that was more permissive than the bridge, which requeues instead of walking).
    """
    global _MARKERS
    if _MARKERS is not None:
        return _MARKERS
    src = src_dir or Path(__file__).resolve().parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from result_markers import dedup_holder_delivered, parse_markers
    _MARKERS = (dedup_holder_delivered, parse_markers)
    return _MARKERS


def result_path(results: Path, task_id: str) -> Path | None:
    """Every shape a DELIVERED result can take, and no shape that is not one.

    Missing a delivered shape yields a false alarm, which costs a re-check;
    admitting a non-delivered shape yields a false all-clear, which costs the
    reply. So the `-` separator on the archive glob is load-bearing: a bare
    `{id}*` prefix also matches `{id}.too-old.<epoch>`, i.e. quarantined.
    `sorted` because filesystem order differs between APFS and the CI runner.
    """
    direct = results / f"{task_id}.txt"
    if direct.exists():
        return direct
    for pat in (f"*.{task_id}.txt",           # per-channel pull namespace
                f"{task_id}.txt.sending",     # claimed mid-delivery
                f"*.{task_id}.txt.sending"):
        hits = sorted(results.glob(pat))
        if hits:
            return hits[0]
    arch = results / "archive"
    hits = sorted(list(arch.glob(f"**/{task_id}.txt")) + list(arch.glob(f"**/{task_id}-*.txt")))
    return hits[0] if hits else None


def task_exists(tasks: Path, task_id: str) -> bool:
    """Did this id ever exist HERE? Task ids are minted per recipient, so a
    peer's id is well-formed and still unresolvable — charset cannot tell."""
    if (tasks / f"{task_id}.txt").exists():
        return True
    arch = tasks / "archive"
    return bool(list(arch.glob(f"**/{task_id}.txt")) + list(arch.glob(f"**/{task_id}-*.txt")))


def task_field(tasks: Path, task_id: str, key: str) -> str | None:
    """One header field of a task, live or archived."""
    for cand in [tasks / f"{task_id}.txt", *sorted((tasks / "archive").glob(f"**/{task_id}*.txt"))]:
        try:
            text = cand.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
        return None
    return None


def read(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def dedup_target(text: str | None, src_dir: Path | None = None) -> str | None:
    """The id a `[deduped:]` points at, or None when this is not a dedup.

    Via `parse_markers`, never a private regex — the grammar has one owner.
    """
    if text is None:
        return None
    _, parse = markers(src_dir)
    action = next((a for a in parse(text).actions
                   if a.kind == "skip" and a.value == "deduped"), None)
    if action is None:
        return None
    return str(action.extra or "").strip()


def dedup_problem(results: Path, task_id: str, tasks: Path | None = None,
                  text: str | None = None, src_dir: Path | None = None) -> str | None:
    """None when the dedup is sound; else why the asking room hears nothing.

    `text` is this task's own result body; omit it and it is read from `results`.
    `tasks` enables the addressee checks — without it they are SKIPPED, not
    passed, so a caller that cannot supply the task dir gets the weaker verdict
    it asked for rather than a silent all-clear.
    """
    delivered, _ = markers(src_dir)
    if text is None:
        text = read(result_path(results, task_id))
    if text is None:
        return "no result file"
    target = dedup_target(text, src_dir)
    if target is None:
        return None          # a real reply, or a deliberate skip for THIS task
    if not target:
        return "deduped into nothing (no target id)"

    if tasks is not None:
        # Sound only within one sender's thread, in one room — neither is
        # visible to the marker grammar, and a cross-room dedup is silent.
        who, to = task_field(tasks, task_id, "user_id"), task_field(tasks, target, "user_id")
        if who and to and who != to:
            return f"CROSS-SENDER: deduped into {target}, which answers {to}, not {who}"
        room = task_field(tasks, task_id, "channel_id")
        dest = task_field(tasks, target, "channel_id")
        if room and dest and room != dest:
            return f"CROSS-ROOM: deduped into {target}, whose reply goes to {dest}, not {room}"

    target_path = result_path(results, target)
    holder = read(target_path)
    if delivered(holder):
        return None
    if target_path is None:
        if tasks is not None and not task_exists(tasks, target):
            return f"DANGLING: deduped into {target}, which does not exist in this workspace"
        return f"ORPHANED: deduped into {target}, which has no result file"
    # `[deduped:]` is itself a skip, so the bridge requeues at the first hop
    # rather than walking the chain — naming the tail says why following it fails.
    chained = dedup_target(holder, src_dir)
    if chained:
        return (f"HOLDER-SKIPPED: deduped into {target}, which is itself [deduped: {chained}] — "
                f"the bridge treats a chained holder as not delivered and requeues; "
                f"it does not walk the chain")
    return f"HOLDER-SKIPPED: deduped into {target}, whose own result is a skip — the bridge requeues this"
