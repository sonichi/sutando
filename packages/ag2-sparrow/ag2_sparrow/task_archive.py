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

import re
from pathlib import Path


# A dot is legal inside an id: pool_lead allows [A-Za-z0-9._~-] and excludes the
# state suffixes by lookahead rather than banning dots.
_ID_STATE = re.compile(r"^(task-.+?)\.(?:assigned|claimed)-.+?\.txt$")
_ID_PLAIN = re.compile(r"^(task-.+?)\.txt(?:\.\d+|\.archive-failed.*)?$")


# A pending file is plain: no state suffix, no archive-retry tail. Kept beside
# the id patterns so a second copy cannot drift from what an id may contain.
_ID_PENDING = re.compile(r"^task-.+?\.txt$")


def is_pending_task_file(name: str) -> bool:
    """True only for an UNCLAIMED, unarchived `task-<id>.txt`.

    A `[^.]+` id class silently drops legal dotted broker ids (`task-a.b`),
    leaving that task pending forever while health stays green.
    """
    return _ID_STATE.match(name) is None and _ID_PENDING.match(name) is not None


def task_id_from_filename(name: str) -> str | None:
    r"""The canonical task id for any name a task file carries, or None.

    `Path.stem` and a greedy `^task-(.+)\.txt$` both return the compound name
    for a CLAIMED file, so the caller then looks for a result under an id that
    nothing ever writes. Covers the lead's `.assigned-<inst>` rename too.
    """
    # _ID_STATE first: the non-greedy id in _ID_PLAIN would otherwise swallow a
    # state suffix and hand back the compound name this function exists to avoid.
    match = _ID_STATE.match(name) or _ID_PLAIN.match(name)
    return match.group(1) if match else None

def find_task_file(tasks_dir: Path, task_id: str) -> Path | None:
    """Return the actual task file path for task_id, or None if absent.

    Checks the bare name first (unclaimed), then any state variant. State
    matching goes through `_ID_STATE` — the same grammar `task_id_from_filename`
    uses — because a second, narrower pattern here is exactly how one state gets
    handled and its sibling missed. If multiple variants exist (shouldn't happen
    but defensive), returns the first lexicographic match; the caller only needs
    one path to archive.
    """
    bare = tasks_dir / f"{task_id}.txt"
    if bare.exists():
        return bare
    matches = sorted(
        p for p in tasks_dir.glob(f"{task_id}.*")
        if (m := _ID_STATE.match(p.name)) and m.group(1) == task_id
    )
    if matches:
        return matches[0]
    # Quarantined last: it is the task's only surviving header block, and
    # routing needs those headers or a failed archive also strands the reply.
    quarantined = sorted(tasks_dir.glob(f"{task_id}.txt.archive-failed*"))
    return quarantined[0] if quarantined else None


# Collision NAMING lives here (_move_without_clobbering mints `.N`); collision
# SELECTION lives with the reader in local_task_protocol. No cross-import: both
# modules are loaded by PATH with src/ off sys.path, where any import of the
# other raises ModuleNotFoundError and takes its caller down with it.


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
