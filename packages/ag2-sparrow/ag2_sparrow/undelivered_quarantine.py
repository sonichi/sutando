#!/usr/bin/env python3
"""Naming and moves for `results/undelivered/` — the delivery quarantine.

The convention (`<stem>-<epoch>.txt`) had exactly one owner, the sparrow bridge,
and only one direction: in. Operator recovery needs the way back, and a second
copy of the name format in a second module is how the two drift. So the format
lives here once and both directions are derived from it.

Every move into or out of the quarantine goes through ONE no-replace transition
(`rename_noreplace`), and the two directions each keep an explicit guarantee:

* into it (`place`): a quarantined copy is never overwritten — the target name is
  allocated fresh and a taken one is skipped, so earlier evidence always survives;
* out of it (`restore`): the exact quarantined body is installed at the live name,
  or it stays quarantined and the refusal is reported. Where the platform has no
  no-replace rename the install is a hard link (it refuses a taken name just as
  atomically) and the quarantined name is then renamed aside, never unlinked: no
  name a producer can retake is ever removed, and the body always keeps a name.

Dependency-light on purpose: no bridge import, no gateway, no env. The caller
supplies the results directory.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import re
import sys
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

DIRNAME = "undelivered"
# `<task stem>-<unix seconds>.txt`; the stem may itself contain hyphens.
_QUARANTINED = re.compile(r"^(?P<stem>.+)-(?P<epoch>\d+)\.txt$")


def quarantine_dir(results_dir: Path) -> Path:
    return Path(results_dir) / DIRNAME


def quarantine_name(stem: str, when: Optional[int] = None) -> str:
    """The one place the quarantined filename is spelled. Nanoseconds, not
    seconds: the drain can quarantine one task several times inside a second."""
    return f"{stem}-{int(when if when is not None else time.time_ns())}.txt"


# ── the one no-replace transition ────────────────────────────────────────────

Log = Callable[[str], None]
# Reported once per process; the lifecycle owner shares this set.
_REPORTED: "set[str]" = set()


def report_once(key: str, log: Log, line: str) -> None:
    """Say `line` once per process for `key`."""
    if key not in _REPORTED:
        _REPORTED.add(key)
        log(line)


def _probe_rename() -> "tuple[str, Optional[Callable[[bytes, bytes], int]]]":
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to speak of
        return "none", None
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):  # pragma: no cover - Linux CI
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return "renamex_np", lambda a, b: fn(a, b, 4)                    # RENAME_EXCL
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        at_fdcwd = -100
        return "renameat2", lambda a, b: fn(at_fdcwd, a, at_fdcwd, b, 1)  # RENAME_NOREPLACE
    if sys.platform == "win32":  # pragma: no cover - os.rename refuses a taken name there
        return "os.rename", None
    return "none", None


RENAME_PRIMITIVE, _RENAME = _probe_rename()


def rename_noreplace(src: Path, dst: Path, log: Optional[Log] = None) -> None:
    """Rename `src` to `dst` only if `dst` does not exist: FileExistsError
    (EEXIST) otherwise, and nothing moved. Without a kernel primitive nothing
    is moved either and FileExistsError carries ENOTSUP: a link-then-unlink
    would have to unlink `src` after a check another writer can invalidate."""
    global RENAME_PRIMITIVE, _RENAME
    if RENAME_PRIMITIVE == "os.rename":  # pragma: no cover - Windows only
        os.rename(src, dst)
        return
    if _RENAME is not None:
        rc = _RENAME(os.fsencode(src), os.fsencode(dst))
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dst))
        if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            RENAME_PRIMITIVE, _RENAME = "none", None   # this filesystem cannot; refuse from now on
        else:
            raise OSError(err, os.strerror(err), str(src))
    report_once("rename:fallback", log or (lambda _m: None),
                "result disposal: no no-replace rename on this platform; a reply that would go "
                "back to its name is kept in undelivered/ instead, never put back")
    raise FileExistsError(errno.ENOTSUP, "no no-replace rename on this platform; nothing moved", str(dst))


def no_primitive(e: OSError) -> bool:
    """The refusal meant "this platform cannot", not "the name is taken"."""
    return isinstance(e, FileExistsError) and e.errno == errno.ENOTSUP


def move_private(src: Path, dst: Path, log: Optional[Log] = None) -> None:
    """Move a body held under a PRIVATE name (one no producer writes) to `dst`,
    never over a file. Without a no-replace rename a hard link refuses a taken
    target just as atomically, and only the private source name is dropped."""
    try:
        rename_noreplace(src, dst, log)
    except FileExistsError as e:
        if not no_primitive(e):
            raise
        os.link(src, dst)
        os.unlink(src)                                  # private: no producer retakes it


def private_name(path: Path, tag: str) -> Path:
    """A sibling nobody else creates, so a plain rename onto it replaces nothing."""
    return Path(path).with_name(f".{Path(path).name}.{tag}-{time.time_ns()}-{uuid.uuid4().hex[:8]}")


# Enough to step past any burst of same-nanosecond copies of one task.
_PLACE_TRIES = 64


def place_as(src: Path, directory: Path, names: Iterable[str],
             move: Optional[Callable[[Path, Path], None]] = None) -> Path:
    """Move `src` into `directory` under the first of `names` nothing holds,
    never replacing a file: `move` must refuse a taken target with
    FileExistsError(EEXIST), and a taken name is skipped for the next."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    move = move or rename_noreplace
    for name in names:
        target = d / name
        try:
            move(Path(src), target)
            return target
        except FileExistsError as e:
            if e.errno not in (None, errno.EEXIST):
                raise
    raise FileExistsError(errno.EEXIST, "no free name", str(d))


def place(src: Path, results_dir: Path, stem: str,
          move: Optional[Callable[[Path, Path], None]] = None,
          when: Optional[int] = None) -> Path:
    """Guarantee (into the quarantine): move `src` there under a name nothing
    holds yet, never replacing a file; a taken name is skipped for the next
    one, so an earlier quarantined copy always survives. Returns the new path."""
    n = int(when if when is not None else time.time_ns())
    return place_as(src, quarantine_dir(results_dir),
                    (quarantine_name(stem, n + i) for i in range(_PLACE_TRIES)), move)


def canonical_result(results_dir: Path, task_id: str) -> Path:
    """The drain's live name for one task's result."""
    stem = f"task-{task_id}" if not str(task_id).startswith("task-") else str(task_id)
    return Path(results_dir) / f"{stem}.txt"


def find_quarantined(results_dir: Path, task_id: str) -> list[Path]:
    """Every quarantined copy of one task, oldest first.

    A task can be quarantined more than once (each failed recovery adds one), so
    the caller decides which to restore rather than being handed a guess.
    """
    d = quarantine_dir(results_dir)
    if not d.is_dir():
        return []
    stem = f"task-{task_id}" if not str(task_id).startswith("task-") else str(task_id)
    out = []
    for p in d.glob("*.txt"):
        m = _QUARANTINED.match(p.name)
        if m and m.group("stem") == stem:
            out.append((int(m.group("epoch")), p))
    return [p for _, p in sorted(out)]


class RestoreOutcome(str, Enum):
    """Absence and refusal are different answers: one means the body is gone,
    the other that a newer reply is already queued. Collapsing them to None
    leaves the operator unable to tell whether they still have a problem."""

    RESTORED = "restored"
    NOTHING_QUARANTINED = "nothing-quarantined"
    LIVE_RESULT_PRESENT = "live-result-present"
    # No atomic no-replace move on this platform: the body stays quarantined.
    NO_SAFE_MOVE = "no-safe-move"


def restore(results_dir: Path, task_id: str) -> "tuple[RestoreOutcome, Optional[Path]]":
    """Guarantee (out of the quarantine): the NEWEST quarantined body is
    installed at the drain's canonical name, or it stays quarantined and the
    outcome says why. A live result is never overwritten (a newer reply waiting
    to go is the one the user should get), and the body never loses its last
    name: nothing a producer can retake is unlinked.
    """
    found = find_quarantined(results_dir, task_id)
    if not found:
        return RestoreOutcome.NOTHING_QUARANTINED, None
    target = canonical_result(results_dir, task_id)
    try:
        rename_noreplace(found[-1], target)
    except FileExistsError as e:
        if not no_primitive(e):
            return RestoreOutcome.LIVE_RESULT_PRESENT, target
        return _restore_by_link(found[-1], target)
    return RestoreOutcome.RESTORED, target


def _restore_by_link(quarantined: Path, target: Path) -> "tuple[RestoreOutcome, Optional[Path]]":
    """No no-replace rename here: a hard link installs the body and refuses a
    taken name atomically; the quarantined name is then renamed aside (a name
    `find_quarantined` does not match), so the body keeps a name either way."""
    try:
        os.link(quarantined, target)
    except FileExistsError:
        return RestoreOutcome.LIVE_RESULT_PRESENT, target
    except OSError:
        return RestoreOutcome.NO_SAFE_MOVE, quarantined
    aside = private_name(quarantined, "restored")
    try:
        os.rename(quarantined, aside)
    except OSError:
        return RestoreOutcome.RESTORED, target   # still listed too: a re-run sees the live name and stops
    if not _held_by_another(target, aside):
        return RestoreOutcome.RESTORED, target
    # A producer replaced the live name after the link: the body is not live, so
    # it goes back where the operator lists it rather than staying under the aside name.
    place(aside, Path(target).parent, Path(target).stem, move=move_private)
    return RestoreOutcome.LIVE_RESULT_PRESENT, target


def _held_by_another(target: Path, body: Path) -> bool:
    """True only when a different file provably holds `target`; a name a drain
    already took holds the body as far as anyone can tell."""
    try:
        return not os.path.samefile(target, body)
    except OSError:
        return False
