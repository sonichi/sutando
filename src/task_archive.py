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
from typing import Callable


# A dot is legal inside an id: pool_lead allows [A-Za-z0-9._~-] and excludes the
# state suffixes by lookahead rather than banning dots.
_STATE_SUFFIX = re.compile(r"^(task-.+?)\.(?:assigned|claimed)-.+$")

# The collision/quarantine tail is appended to the WHOLE name, so the structural
# .txt is the rightmost one: greedy, or a long id's quarantine re-aliases.
_NOT_A_RECORD = re.compile(r"^(.+)\.txt(?:\.\d+|\.archive-failed.*)$")
_DECLARED_ID = re.compile(r"^id:[ \t]*(\S+)[ \t]*$", re.M)


def _stem_of(name: str) -> str | None:
    if name.endswith(".txt"):
        return name[:-4] or None
    m = _NOT_A_RECORD.match(name)
    return m.group(1) if m else None


def task_id_from_filename(name: str) -> str | None:
    r"""The canonical task id for any name a task file carries, or None.

    `Path.stem` and a greedy `^task-(.+)\.txt$` both return the compound name
    for a CLAIMED file, so the caller then looks for a result under an id that
    nothing ever writes. Covers the lead's `.assigned-<inst>` rename too.
    """
    stem = _stem_of(name)
    # An LF is a legal filename byte but not an id byte: the grammar rejects it.
    if stem is None or "\n" in stem or not stem.startswith("task-"):
        return None
    m = _STATE_SUFFIX.match(stem)
    return m.group(1) if m else stem


def archive_id_from_filename(name: str) -> str | None:
    """The id an ARCHIVED file carries, whatever its producer prefix, or None.

    `task_id_from_filename` is deliberately anchored to the live `task-*`
    namespace; the archive is not, so a history reader that only knows the
    live grammar drops every legacy row. Callers gate the result with
    `local_task_protocol.valid_archive_lookup_id`.
    """
    task_id = task_id_from_filename(name)
    if task_id is not None:
        return task_id
    return _stem_of(name)


def declared_task_id(path: Path) -> str | None:
    """The `id:` a task file persists in its header block, or None."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    head = text.split("\ntask:", 1)[0]
    m = _DECLARED_ID.search(head)
    return m.group(1) if m else None


def task_id_for(path: Path, *, accept: Callable[[str], bool] | None = None) -> str | None:
    """The canonical id of a task file, live or quarantined, or None.

    The persisted `id:` is the positive authority: a gateway id may itself end
    in `.claimed-<x>`, and only the header tells it from a pool rename. The
    filename answers only for a file that declares nothing (or nothing
    `accept` admits). `accept` is the caller's id grammar; this module has none.
    """
    declared = declared_task_id(path)
    if declared is not None and (accept is None or accept(declared)):
        return declared
    parsed = archive_id_from_filename(Path(path).name)
    if parsed is None or (accept is not None and not accept(parsed)):
        return None
    return parsed


def lookup_id_from_filename(name: str) -> str:
    """The archive-lookup id for a filename, with a bare id passing through.

    `archive_id_from_filename` answers only for a name carrying a structural
    `.txt`; a bare id has none, and dots are legal inside ids, so it is
    returned unchanged rather than run through a stem.
    """
    task_id = archive_id_from_filename(name)
    return task_id if task_id is not None else name


def find_task_file(tasks_dir: Path, task_id: str) -> Path | None:
    """Return the actual task file path for task_id, or None if absent.

    Checks the bare name first (unclaimed), then every variant — pool state or
    quarantine — through the one predicate `task_id_for`, so the persisted id
    is the same authority for a quarantined file as for a live one. A live
    variant outranks a quarantine; ties fall to the first lexicographic name.
    """
    bare = tasks_dir / f"{task_id}.txt"
    if bare.exists():
        return bare
    matches = sorted(
        (p for p in tasks_dir.glob(f"{task_id}.*")
         if p.name != bare.name and task_id_for(p) == task_id),
        # A quarantine has left the .txt glob; it answers only when no live
        # variant does, since routing still needs its surviving header block.
        key=lambda p: (not p.name.endswith(".txt"), p.name),
    )
    return matches[0] if matches else None


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
