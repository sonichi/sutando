#!/usr/bin/env python3
"""Tasks that were handled but never got a result file.

A task file stays in `tasks/` until a result is written and the bridge archives
it, so the queue is already the record of what is unanswered. Nothing reads it
at the END of a pass, though, and the miss is invisible from inside: the agent
answers in its own transcript, the terminal shows the reply, and only the queue
disagrees. Measured five times in one session, caught every time by re-listing
by hand and never by recall.

Exit 1 when a task older than --min-age-sec has no result, 0 otherwise.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _result_path(results: Path, task_id: str) -> Path | None:
    """Every shape a delivered result can take (CLAUDE.md 'Result-body protocol').

    Missing one shape produces a FALSE ALARM, which is the safe direction here —
    it costs a re-check, where a false all-clear costs the reply itself.
    `sorted` because an unordered glob picks by filesystem order, which differs
    between APFS and the CI runner.
    """
    direct = results / f"{task_id}.txt"
    if direct.exists():
        return direct
    for pat in (f"*.{task_id}.txt",                      # per-channel pull namespace
                f"{task_id}.txt.sending",                # claimed mid-delivery
                f"*.{task_id}.txt.sending"):
        hits = sorted(results.glob(pat))
        if hits:
            return hits[0]
    # `{id}-*` requires the separator: a bare `{id}*` prefix also matches
    # `{id}.too-old.<epoch>`, i.e. QUARANTINED, which is the opposite of delivered.
    arch = results / "archive"
    hits = sorted(list(arch.glob(f"**/{task_id}.txt")) + list(arch.glob(f"**/{task_id}-*.txt")))
    return hits[0] if hits else None


def _task_exists(tasks: Path, task_id: str) -> bool:
    """Did this id ever exist HERE? Task ids are minted per recipient, so a
    peer's id is well-formed and still unresolvable — charset cannot tell."""
    if (tasks / f"{task_id}.txt").exists():
        return True
    arch = tasks / "archive"
    return bool(list(arch.glob(f"**/{task_id}.txt")) + list(arch.glob(f"**/{task_id}-*.txt")))


def _task_field(tasks: Path, task_id: str, key: str) -> str | None:
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


_MARKERS = None


def _markers():
    """The marker grammar is centralised in src/result_markers.py (CLAUDE.md);
    re-implementing it drifts from what the bridge actually does."""
    global _MARKERS
    if _MARKERS is not None:
        return _MARKERS
    repo = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root (sys.path only; the root comes from the CLI arg)
    sys.path.insert(0, str(repo / "src"))
    try:
        from result_markers import dedup_holder_delivered, parse_markers
    except ImportError as exc:
        print(f"unanswered-tasks: cannot import src/result_markers.py ({exc}) — "
              "refusing to re-implement the marker grammar", file=sys.stderr)
        raise SystemExit(2) from exc
    _MARKERS = (dedup_holder_delivered, parse_markers)
    return _MARKERS


def _read(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _unanswered_reason(results: Path, task_id: str, tasks: Path | None = None) -> str | None:
    """None when the room heard something; else why it did not.

    Delegates the `[deduped:]` verdict to `dedup_holder_delivered`, which is
    what the bridge asks — a holder that is itself a skip never delivered.
    """
    delivered, parse = _markers()
    text = _read(_result_path(results, task_id))
    if text is None:
        return "no result file"
    dedup = next((a for a in parse(text).actions
                  if a.kind == "skip" and a.value == "deduped"), None)
    if dedup is None:
        return None  # a real reply, or a deliberate skip decision for THIS task
    target = str(dedup.extra or "").strip()
    if not target:
        return "deduped into nothing (no target id)"
    if tasks is not None:
        # The dedup is sound only within ONE SENDER's thread. Comparing the CHANNEL
        # misses every case in a shared room, which is where peers actually talk.
        who = _task_field(tasks, task_id, "user_id")
        to = _task_field(tasks, target, "user_id")
        if who and to and who != to:
            return f"CROSS-SENDER: deduped into {target}, which answers {to}, not {who}"
        room, dest_room = _task_field(tasks, task_id, "channel_id"), _task_field(tasks, target, "channel_id")
        if room and dest_room and room != dest_room:
            return f"CROSS-ROOM: deduped into {target}, whose reply goes to {dest_room}, not {room}"
    target_path = _result_path(results, target)
    if delivered(_read(target_path)):
        return None
    if target_path is None:
        if tasks is not None and not _task_exists(tasks, target):
            return f"DANGLING: deduped into {target}, which does not exist in this workspace"
        return f"ORPHANED: deduped into {target}, which has no result file"
    return f"HOLDER-SKIPPED: deduped into {target}, whose own result is a skip — the bridge requeues this"


def unanswered(workspace: Path, min_age_sec: float, now: float | None = None) -> list[tuple[str, float, str]]:
    _markers()  # resolve up front: an empty queue must not silently skip the guard
    now = time.time() if now is None else now
    tasks, results = workspace / "tasks", workspace / "results"
    out: list[tuple[str, float, str]] = []
    if not tasks.is_dir():
        return out
    for f in sorted(tasks.glob("task-*.txt")):
        age = now - f.stat().st_mtime
        if age < min_age_sec:
            continue  # still plausibly in flight
        reason = _unanswered_reason(results, f.stem, tasks)
        if reason is not None:
            out.append((f.stem, age, reason))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--min-age-sec", type=float, default=120.0,
                    help="ignore tasks younger than this (default 120)")
    a = ap.parse_args()
    rows = unanswered(Path(a.workspace), a.min_age_sec)
    if not rows:
        print("unanswered-tasks: none")
        return 0
    for task_id, age, reason in rows:
        print(f"UNANSWERED {task_id} ({age / 60:.1f}m old) — {reason}; the room heard nothing")
    return 1


if __name__ == "__main__":
    sys.exit(main())
