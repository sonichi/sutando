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


def _move_without_clobbering(src: Path, dest: Path, *, src_dir_fd: "int | None" = None,
                             dst_dir_fd: "int | None" = None,
                             expect: "str | None" = None) -> Path:
    """Move src to dest, or to dest.N if taken. Returns where it landed.
    link()+unlink(): rename()/move() REPLACE on POSIX — data loss on a repeat.
    With directory handles, src/dest are names inside them and no path is re-resolved;
    `expect` (a `file_generation`) moves only that generation and retires the source
    into the archive's `.retired/` instead of unlinking it (`retire`)."""
    if src_dir_fd is not None or dst_dir_fd is not None:
        return _move_relative(src, dest, src_dir_fd, dst_dir_fd, expect)
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


def _move_relative(src: Path, dest: Path, src_dir_fd: "int | None", dst_dir_fd: "int | None",
                   expect: "str | None" = None) -> Path:
    """The handle form of `_move_without_clobbering`: same policy, every call
    relative to the directories, unlink of the (private) source by name in its handle."""
    import os
    import shutil
    import stat as _stat
    import uuid
    base, candidate, n = dest, dest, 0
    while True:
        try:
            os.link(str(src), str(candidate), src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            if expect is not None and file_generation(os.lstat(str(candidate), dir_fd=dst_dir_fd)) != expect:
                with retired_dir(dst_dir_fd) as rfd:  # src changed after it was judged: set aside
                    retire(str(candidate), src_dir_fd=dst_dir_fd, retired_fd=rfd, expect=None)
                raise PlacementMismatch(str(src))
            break
        except FileExistsError:
            n += 1
            candidate = base.with_name(f"{base.name}.{n}")
        except PlacementMismatch:
            raise
        except OSError:
            tmp = base.with_name(f".{base.name}.{uuid.uuid4().hex[:12]}.part")
            fd_in = os.open(str(src), os.O_RDONLY, dir_fd=src_dir_fd)
            if expect is not None and file_generation(os.fstat(fd_in)) != expect:
                os.close(fd_in)
                raise PlacementMismatch(str(src))
            try:
                fd_out = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                                 dir_fd=dst_dir_fd)
            except BaseException:
                os.close(fd_in)
                raise
            try:
                with os.fdopen(fd_out, "wb") as out, os.fdopen(fd_in, "rb") as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    st = os.fstat(inp.fileno())
                    os.fchmod(out.fileno(), _stat.S_IMODE(st.st_mode))
                    os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                    os.fsync(out.fileno())
                while True:
                    try:
                        os.link(str(tmp), str(candidate), src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
                        break
                    except FileExistsError:
                        n += 1
                        candidate = base.with_name(f"{base.name}.{n}")
            finally:
                os.unlink(str(tmp), dir_fd=dst_dir_fd)
            break
    try:  # landed: from here a failure must not read as "not placed"
        if dst_dir_fd is not None:  # the landing is durable before the source goes
            _fsync_dir(dst_dir_fd)
        _checkpoint("after_link", str(src))
        if expect is None:
            os.unlink(str(src), dir_fd=src_dir_fd)
            if src_dir_fd is not None:
                _fsync_dir(src_dir_fd)
        else:
            with retired_dir(dst_dir_fd) as rfd:
                stray = retire(str(src), src_dir_fd=src_dir_fd, retired_fd=rfd, expect=expect)
            if stray:
                raise PlacementSourceReplaced(str(candidate), f"archive/{RETIRED_DIR}/{stray}")
    except OSError as exc:
        raise PlacementUnsynced(str(candidate), exc) from exc
    return candidate


RETIRED_DIR = ".retired"


class retired_dir:
    """The archive's `.retired/` (created when missing) as a directory handle."""

    def __init__(self, archive_fd: int):
        self.archive_fd = archive_fd

    def __enter__(self) -> int:
        import os
        try:
            os.mkdir(RETIRED_DIR, 0o700, dir_fd=self.archive_fd)
        except FileExistsError:
            pass
        self.fd = os.open(RETIRED_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                          | getattr(os, "O_NOFOLLOW", 0), dir_fd=self.archive_fd)
        return self.fd

    def __exit__(self, *exc) -> None:
        import os
        os.close(self.fd)


def retired_names(name: str, retired_fd: int) -> "list[str]":
    """Where `retire` may have put `name`: `name` and its `.N`, in order."""
    import os
    import re
    tail = re.compile(re.escape(name) + r"(?:\.([1-9][0-9]*))?")
    return [e for _, e in sorted((int(m.group(1) or 0), e) for e in os.listdir(retired_fd)
                                 for m in [tail.fullmatch(e)] if m)]


def retire(name: str, *, src_dir_fd: int, retired_fd: int, expect: "str | None") -> "str | None":
    """The one way this module removes a task-bearing name: rename it into
    `.retired/` under `name` (or the first free `name.N`, so a re-entry never
    overwrites), sync both directories, and judge what moved. Nothing is unlinked.
    Returns None when the retired file is the `expect` generation, else its retired
    name, kept for the operator. FileNotFoundError when `name` is gone."""
    import errno
    import os
    try:
        from . import undelivered_quarantine as _uq
    except ImportError:
        import undelivered_quarantine as _uq
    taken, n = set(retired_names(name, retired_fd)), 0
    while True:
        as_name = name if n == 0 else f"{name}.{n}"
        n += 1
        if as_name in taken:
            continue
        _checkpoint("before_retire", name)
        try:  # atomic no-replace: a name taken since the listing is skipped, never overwritten
            _uq.renameat_noreplace(name, as_name, src_dir_fd=src_dir_fd, dst_dir_fd=retired_fd)
            break
        except FileExistsError as exc:
            if exc.errno != errno.EEXIST:
                raise OSError(errno.ENOTSUP, f"cannot retire {name} without overwriting: {exc}") from exc
    _checkpoint("after_retire", name)
    _fsync_dir(retired_fd)
    _fsync_dir(src_dir_fd)
    if expect is not None and file_generation(os.lstat(as_name, dir_fd=retired_fd)) == expect:
        return None
    return as_name


# The one owner of collision naming, callable from outside the archive path too.
move_without_clobbering = _move_without_clobbering


def _checkpoint(point: str, name: str) -> None:
    """A named injection point for tests (`before_give_back`, `after_link`, `before_retire`,
    `after_retire`); a no-op in production."""


def _fsync_dir(fd: int) -> None:
    """A rename or link is durable only once its directory is: without this, power
    loss after the call can strand a claim the journal already names."""
    import os
    if os.name != "nt":
        os.fsync(fd)


def find_landed(dest: str, generation: str, *, dst_dir_fd: int) -> "str | None":
    """The name among `dest` and every `dest.N` actually present (the `.N` names
    `move_without_clobbering` mints) that holds `generation`, or None. Gaps are
    normal once an older record is requeued, so the whole directory is read."""
    import os
    import re
    tail = re.compile(re.escape(dest) + r"(?:\.([1-9][0-9]*))?")
    found = sorted((int(m.group(1) or 0), e) for e in os.listdir(dst_dir_fd)
                   for m in [tail.fullmatch(e)] if m)
    for _, candidate in found:
        try:
            st = os.lstat(candidate, dir_fd=dst_dir_fd)
        except FileNotFoundError:
            continue
        if file_generation(st) == generation:
            return candidate
    return None


def file_generation(st) -> str:
    """The identity of a file's bytes: device, inode, size, mtime_ns. A rewrite
    under the same name is a new generation; that is how the watcher tells a
    re-emitted task from a replaced one."""
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


class PlacementMismatch(Exception):
    """The name to move no longer holds the inode that was judged; nothing was placed."""

    def __init__(self, name: str):
        super().__init__(f"{name} was replaced after it was judged")
        self.name = name


class PlacementSourceReplaced(Exception):
    """Placed at `landed`, but the source name now held another inode, kept as `kept_as`."""

    def __init__(self, landed: str, kept_as: str):
        super().__init__(f"placed as {landed}; the source was replaced and is kept as {kept_as}")
        self.landed, self.kept_as = landed, kept_as


class PlacementUnsynced(Exception):
    """The file is linked at `landed` in the archive, but making that durable or
    removing the source failed: it is placed, not given back, and must be finished."""

    def __init__(self, landed: str, cause: OSError):
        super().__init__(f"landed as {landed}, not yet durable ({cause})")
        self.landed, self.cause = landed, cause


class GenerationChanged(Exception):
    """What held the name was not the planned generation. It is back under its
    name, live (`kept_as` None), or kept under its private claim name when the
    name had been taken again meanwhile (`kept_as` that name)."""

    def __init__(self, name: str, why: str, kept_as: "str | None" = None):
        super().__init__(f"{name}: {why}")
        self.name, self.why, self.kept_as = name, why, kept_as


def _give_back(claim: str, name: str, why: str, dir_fd: int, *, archive_fd: int,
               expect: "str | None" = None) -> GenerationChanged:
    """Link the claim back to its name without replacing whatever holds it now,
    then retire the claim name (`retire`, never an unlink). A live name that does
    not hold `expect` after the link is set aside too. Returns the error to raise."""
    import os
    _checkpoint("before_give_back", name)
    try:
        os.link(claim, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except FileExistsError:
        return GenerationChanged(name, why, kept_as=claim)
    _fsync_dir(dir_fd)
    live = file_generation(os.lstat(name, dir_fd=dir_fd))
    with retired_dir(archive_fd) as rfd:
        if expect is not None and live != expect:  # the claim was replaced before the link
            moved = retire(name, src_dir_fd=dir_fd, retired_fd=rfd, expect=None)
            return GenerationChanged(name, f"{why}; its claim was replaced, so that file is kept as "
                                           f"archive/{RETIRED_DIR}/{moved}", kept_as=claim)
        stray = retire(claim, src_dir_fd=dir_fd, retired_fd=rfd, expect=live)
    if stray:
        return GenerationChanged(name, f"{why}; a file that replaced its claim is kept as "
                                       f"archive/{RETIRED_DIR}/{stray}")
    return GenerationChanged(name, why)


give_back = _give_back


def _keep_displaced(fd: int, claim: str, dir_fd: int) -> "str | None":
    """The judged bytes, whose name was taken from under the move, copied to a
    fresh `<claim>.displaced` from the descriptor still holding them."""
    import os
    keep = f"{claim}.displaced"
    try:
        out = os.open(keep, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(out, "wb") as fh:
        offset = 0
        while chunk := os.pread(fd, 1 << 16, offset):
            fh.write(chunk)
            offset += len(chunk)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(dir_fd)
    return keep


def claim_name(name: str) -> str:
    """A fresh private claim name for `name`: a caller that journals its moves
    records this BEFORE `claim_generation` renames to it, so a crash in between
    leaves a claim recovery can attribute."""
    import os
    import secrets
    return f"{name}.claim-{os.getpid()}-{secrets.token_hex(5)}"


def claim_generation(name: str, *, src_dir_fd: int, claim: "str | None" = None) -> str:
    """Take `name` (a bare name inside `src_dir_fd`) by one atomic rename into a
    private claim beside it, same directory, same filesystem — never a copy.
    Returns the claim name (`claim` when given, else a fresh `claim_name`);
    FileNotFoundError when the name is gone. The claim holds whatever occupied
    the name at that instant: judge it with `place_claim`."""
    import os
    claim = claim or claim_name(name)
    os.rename(name, claim, src_dir_fd=src_dir_fd, dst_dir_fd=src_dir_fd)
    _fsync_dir(src_dir_fd)
    return claim


def place_claim(claim: str, name: str, generation: str, dest: str, *, src_dir_fd: int,
                dst_dir_fd: int) -> Path:
    """Judge the claim through its own descriptor against `generation`
    (`file_generation`) and, only on a match, place it at `dest` (or `dest.N`)
    inside `dst_dir_fd` by `move_without_clobbering` through the two handles;
    returns the landed name. A mismatch (or a failed placing move) gives the
    claim back to `name` without replacing — link, then unlink the private
    claim — and raises GenerationChanged; when `name` was retaken meanwhile the
    claim stays put as the quarantine, named in the error's `kept_as`. No path
    is re-resolved and no live name is ever unlinked."""
    import os
    try:
        fd = os.open(claim, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=src_dir_fd)
    except OSError as exc:
        raise _give_back(claim, name, f"cannot be opened as a regular file ({exc})", src_dir_fd,
                         archive_fd=dst_dir_fd) from None
    try:
        judged = file_generation(os.fstat(fd))
        if judged != generation:
            raise _give_back(claim, name, "changed since it was planned", src_dir_fd,
                             archive_fd=dst_dir_fd, expect=judged)
        try:
            return move_without_clobbering(Path(claim), Path(dest), src_dir_fd=src_dir_fd,
                                           dst_dir_fd=dst_dir_fd, expect=judged)
        except PlacementMismatch as exc:
            exc.kept_as = _keep_displaced(fd, claim, src_dir_fd)
            raise
        except OSError as exc:
            raise _give_back(claim, name, f"the archive move failed ({exc})", src_dir_fd,
                             archive_fd=dst_dir_fd, expect=judged) from exc
    finally:
        os.close(fd)


def move_generation(name: str, generation: str, dest: str, *, src_dir_fd: int,
                    dst_dir_fd: int) -> Path:
    """`claim_generation` then `place_claim` in one call: move exactly the planned
    generation of `name` to `dest` (or `dest.N`), never unlinking a live name.
    A caller that must survive a crash between the two persists the claim name
    in between and calls them separately."""
    claim = claim_generation(name, src_dir_fd=src_dir_fd)
    return place_claim(claim, name, generation, dest, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


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
