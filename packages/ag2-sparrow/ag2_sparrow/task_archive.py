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
    if matches:
        return matches[0]
    # Quarantined last: archive_file() mints this name when it cannot archive,
    # and it is still the task's only surviving header block. Without it a
    # failed archive also strands the reply, since routing needs the headers.
    quarantined = sorted(tasks_dir.glob(f"{task_id}.txt.archive-failed*"))
    return quarantined[0] if quarantined else None


def _move_without_clobbering(src: Path, dest: Path) -> Path:
    """Move src to dest, or to dest.N if taken. Returns where it landed.

    link()+unlink() rather than rename()/move(): those REPLACE an existing
    destination on POSIX, which is data loss on a repeated task id.
    """
    import os
    import shutil
    base, candidate, n = dest, dest, 0
    while True:
        try:
            os.link(str(src), str(candidate))
            break
        except FileExistsError:
            n += 1
            candidate = base.with_name(f"{base.name}.{n}")
        except OSError:
            # Cross-device: link() can't span filesystems. O_EXCL still refuses
            # an existing destination, so the no-clobber guarantee survives.
            while True:
                try:
                    fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    n += 1
                    candidate = base.with_name(f"{base.name}.{n}")
                    continue
                try:
                    with open(fd, "wb") as out, open(src, "rb") as inp:
                        shutil.copyfileobj(inp, out)
                    shutil.copystat(str(src), str(candidate))
                except BaseException:
                    os.unlink(str(candidate))
                    raise
                break
            break
    src.unlink()
    return candidate


def archive_file(src: Path, kind: str, task_id: str, *,
                 tasks_dir: Path, results_dir: Path, log=print) -> bool:
    """Move src into the archive, NEVER deleting or overwriting a record.

    True when src has left the live queue (archived, quarantined, or never
    existed); False only when it is still there under its live name.
    """
    from datetime import datetime
    try:
        if src.exists():
            base = tasks_dir if kind == "tasks" else results_dir
            dest_dir = base / datetime.now().strftime("%Y-%m")
            dest_dir.mkdir(parents=True, exist_ok=True)
            _move_without_clobbering(src, dest_dir / f"{task_id}.txt")
        return True
    except Exception as e:
        log(f"  archive_file({kind}, {task_id}) failed: {e}")
    try:
        # The suffix leaves the *.txt glob so the file stops being polled.
        dest = _move_without_clobbering(
            src, src.with_suffix(src.suffix + ".archive-failed"))
        log(f"  archive_file({kind}, {task_id}) quarantined as {dest.name}")
        return True
    except Exception as e:
        log(f"  archive_file({kind}, {task_id}) STILL in the live queue, expect reprocessing: {e}")
        return False
