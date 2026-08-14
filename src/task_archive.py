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
    # Quarantined last: it is the task's only surviving header block, and
    # routing needs those headers or a failed archive also strands the reply.
    quarantined = sorted(tasks_dir.glob(f"{task_id}.txt.archive-failed*"))
    return quarantined[0] if quarantined else None


def newest_archived(directory: Path, task_id: str) -> Path | None:
    """Newest record for task_id in one directory — collision suffix included.
    A repeat lands as `<id>.txt.1`, so plain `<id>.txt` is the OLDEST, not current."""
    base = directory / f"{task_id}.txt"
    if not base.exists():
        return None          # `.N` is only minted once `.txt` is taken
    # Probe exact names, never glob: this runs in agent-api's per-poll loop over an
    # archive dir that reached 5,716 entries, where a glob measured 442x an exists().
    best, n = base, 1
    while True:
        nxt = base.with_name(f"{base.name}.{n}")
        if not nxt.exists():
            return best
        best, n = nxt, n + 1


def _move_without_clobbering(src: Path, dest: Path) -> Path:
    """Move src to dest, or to dest.N if taken. Returns where it landed.
    link()+unlink(): rename()/move() REPLACE on POSIX — data loss on a repeat."""
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
            # Cross-device: link() can't span filesystems. Fill a private temp
            # first — the authoritative name created early publishes a stub on a kill.
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(base.parent),
                                       prefix=f".{base.name}.", suffix=".part")
            try:
                with open(fd, "wb") as out, open(src, "rb") as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
                shutil.copystat(str(src), tmp)
                while True:
                    try:
                        os.link(tmp, str(candidate))   # atomic, refuses existing
                        break
                    except FileExistsError:
                        n += 1
                        candidate = base.with_name(f"{base.name}.{n}")
            finally:
                os.unlink(tmp)
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
