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


def archive_month(when: float | None = None) -> str:
    """Month partition the archive writes into, in the LOCAL calendar.

    A reader computing this in UTC misses the writer's partition for the hours
    either side of a month boundary, and reads a delivered reply as absent.
    """
    from datetime import datetime
    moment = datetime.fromtimestamp(when) if when is not None else datetime.now()
    return moment.strftime("%Y-%m")


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
            dest_dir = base / archive_month()
            dest_dir.mkdir(parents=True, exist_ok=True)
            _move_without_clobbering(src, dest_dir / f"{task_id}.txt")
            if kind == "tasks":
                retire_delivery_pointers(deliveries_root_for(src.parent), task_id, log=log)
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


# A worker inbox holds a 0-byte pointer per task routed to it:
# deliveries/<recipient>/<task-id><suffix>. Same names and lock as the pool's own writer.
POINTER_SUFFIXES = (".txt", ".accepted", ".claimed")
POINTER_LOCK_NAME = ".lock"
POINTER_ARCHIVE_DIR = "archive"
_POINTER_LOCK_WAIT_S = 2.0


def deliveries_root_for(tasks_dir: Path) -> Path:
    """The pointer root beside a workspace's live task dir."""
    return Path(tasks_dir).parent / "deliveries"


def _regular_at(name: str, dir_fd: int) -> bool:
    import os
    import stat
    try:
        return stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _retire_in_folder(folder: str, task_id: str) -> list[str]:
    """Rename this inbox's pointers for `task_id` into its archive/, under its lock."""
    import fcntl
    import os
    import stat
    import time
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    try:
        if not any(_regular_at(task_id + s, dfd) for s in POINTER_SUFFIXES):
            return []
        lfd = os.open(POINTER_LOCK_NAME, os.O_RDWR | os.O_CREAT | nofollow, 0o644, dir_fd=dfd)
        try:
            if not stat.S_ISREG(os.fstat(lfd).st_mode):
                raise OSError(f"{POINTER_LOCK_NAME} is not a regular file")
            deadline = time.monotonic() + _POINTER_LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            try:
                os.mkdir(POINTER_ARCHIVE_DIR, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            afd = os.open(POINTER_ARCHIVE_DIR, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=dfd)
            moved = []
            try:
                for name in (task_id + s for s in POINTER_SUFFIXES):
                    if _regular_at(name, dfd):
                        os.rename(name, name, src_dir_fd=dfd, dst_dir_fd=afd)
                        moved.append(os.path.join(folder, POINTER_ARCHIVE_DIR, name))
            finally:
                os.close(afd)
            return moved
        finally:
            os.close(lfd)
    finally:
        os.close(dfd)


def retire_delivery_pointers(deliveries_root: Path, task_id: str, *, log=print) -> list[str]:
    """Move every inbox's pointer for an archived task into that inbox's archive/.

    Called from the step that archives the task body, so "this task is done" has one
    writer. Best-effort: a failure is logged and the next migration pass retries it.
    """
    import os
    if not task_id or "/" in task_id or task_id in (".", ".."):
        return []
    try:
        with os.scandir(deliveries_root) as entries:
            folders = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return []
    except OSError as e:
        log(f"  retire pointers for {task_id}: cannot list {deliveries_root}: {e}")
        return []
    moved: list[str] = []
    for folder in folders:
        try:
            moved += _retire_in_folder(folder, task_id)
        except OSError as e:
            log(f"  retire pointer for {task_id} in {folder} failed: {e}")
    return moved


def _ids_in(directory: Path) -> set[str]:
    import os
    try:
        with os.scandir(directory) as entries:
            names = [e.name for e in entries]
    except OSError:
        return set()
    return {i for i in (task_id_from_filename(n) for n in names) if i}


def retire_archived_pointers(workspace: Path, *, log=print) -> dict:
    """One-time, idempotent migration: retire pointers whose task body is archived.

    A pointer whose body is still live in tasks/ is pending and is never touched.
    """
    import os
    import re
    tasks = Path(workspace) / "tasks"
    live = _ids_in(tasks)
    archive = tasks / "archive"
    archived = _ids_in(archive) | _ids_in(tasks / "processed")
    try:
        with os.scandir(archive) as entries:
            months = [e.path for e in entries
                      if re.fullmatch(r"\d{4}-\d{2}", e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        months = []
    for m in months:
        archived |= _ids_in(Path(m))
    root = deliveries_root_for(tasks)
    counts = {"retired": 0, "kept_pending": 0, "kept_unarchived": 0}
    try:
        with os.scandir(root) as entries:
            folders = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return counts
    for folder in folders:
        ids = set()
        with os.scandir(folder) as entries:
            for e in entries:
                for suffix in POINTER_SUFFIXES:
                    if e.name.startswith("task-") and e.name.endswith(suffix):
                        ids.add(e.name[: -len(suffix)])
                        break
        for task_id in sorted(ids):
            if task_id in live:
                counts["kept_pending"] += 1
            elif task_id not in archived:
                counts["kept_unarchived"] += 1
            else:
                try:
                    counts["retired"] += len(_retire_in_folder(folder, task_id))
                except OSError as e:
                    log(f"  retire pointer for {task_id} in {folder} failed: {e}")
    return counts


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != "retire-archived-pointers":
        print("usage: task_archive.py retire-archived-pointers <workspace>", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(retire_archived_pointers(Path(sys.argv[2]),
                                              log=lambda m: print(m, file=sys.stderr))))
