"""Task-file locator for archive calls (#933).

claim_task.py (#884) renames task-{id}.txt → task-{id}.claimed-core-N.txt
when a core claims work. Bridge archive calls that hard-code the bare
task-{id}.txt path silently no-op after claiming, leaving stranded
.claimed-core-N.txt files in tasks/ forever.

Usage:
    from task_archive import find_task_file

    task_file = find_task_file(TASKS_DIR, task_id)
    if task_file:
        archive_file(task_file, "tasks", task_id)
"""
from __future__ import annotations

from pathlib import Path


def find_task_file(tasks_dir: Path, task_id: str) -> Path | None:
    """Return the actual task file path for task_id, or None if absent.

    Checks the bare name first (unclaimed), then globs for the claimed
    variant (task-{id}.claimed-core-N.txt). If multiple claimed variants
    exist (shouldn't happen but defensive), returns the first lexicographic
    match and that's good enough — the caller only needs one path to archive.
    """
    bare = tasks_dir / f"{task_id}.txt"
    if bare.exists():
        return bare
    matches = sorted(tasks_dir.glob(f"{task_id}.claimed-core-*.txt"))
    return matches[0] if matches else None


def archive_file(src: Path, kind: str, task_id: str, *,
                 tasks_dir: Path, results_dir: Path, log=print) -> bool:
    """Move src into the archive, NEVER deleting it if that fails.

    Returns True when src has left the live queue (archived, quarantined, or
    never existed), False only when it is still there under its live name.

    Adapters inject their own resolved destinations and logger; the
    never-delete policy lives here so the three bridges cannot drift apart on
    it. A failed archive that unlinks the source destroys the only copy of a
    task, which is unrecoverable; a stale file is merely noisy.
    """
    import shutil
    from datetime import datetime
    try:
        if src.exists():
            base = tasks_dir if kind == "tasks" else results_dir
            dest_dir = base / datetime.now().strftime("%Y-%m")
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest_dir / f"{task_id}.txt"))
        return True
    except Exception as e:
        log(f"  archive_file({kind}, {task_id}) failed: {e}")
    try:
        # Never delete, and never overwrite: rename() replaces an existing file
        # on POSIX, so a repeated id would destroy the earlier quarantine.
        base = src.with_suffix(src.suffix + ".archive-failed")
        dest, n = base, 0
        while dest.exists():
            n += 1
            dest = base.with_name(f"{base.name}.{n}")
        # link() REFUSES an existing dest, so a lost race errors instead of
        # clobbering; the suffix leaves the *.txt glob so it stops being polled.
        import os as _os
        _os.link(str(src), str(dest))
    except Exception as e:
        log(f"  archive_file({kind}, {task_id}) STILL in the live queue, expect reprocessing: {e}")
        return False
    try:
        src.unlink()
        log(f"  archive_file({kind}, {task_id}) quarantined as {dest.name}")
        return True
    except Exception as e:
        log(f"  archive_file({kind}, {task_id}) STILL in the live queue, expect reprocessing: {e}")
        return False
