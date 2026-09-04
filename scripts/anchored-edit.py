#!/usr/bin/env python3
"""Anchored in-place edit that REFUSES rather than silently doing nothing.

The failure this exists for: an ad-hoc `s.replace(old, new)` whose anchor has
drifted matches nothing, writes the file back unchanged, and the surrounding
script prints its own success message anyway — because that message was
generated independently of the operation. `echo updated` proves the echo ran.

Measured 2026-09-04, three times across two agents in one session: a patch
script that printed "second copy updated" against an anchor the file did not
contain; a build_log append whose redirect was dropped, rendering to the
terminal; and two task closures narrated with no result file written. Every
one reported success.

So the receipt here is derived from the file AFTER the write, and every
refusal is an exit code:

    0  the file changed, and the receipt counts the new text in it
    2  the anchor is absent, ambiguous, or the edit is a no-op

CONCURRENCY, AND ITS BOUNDARY — this is a cooperative protocol, not a
guarantee against arbitrary writers. The read, the compute and the replace are
held under an exclusive flock on `<file>.anchored-lock`, so two writers that
BOTH take that lock cannot interleave and neither can lose an update. A writer
that ignores the lock is outside the protocol: the pre-replace reread narrows
the window it can land in, but does not close it, and such an update can still
be overwritten. Take `<file>.anchored-lock` if you edit these files from
elsewhere. Do not read this tool as making arbitrary post-read updates safe.

Usage:
    anchored-edit.py FILE --old-file OLD --new-file NEW [--count N] [--allow-multi]
    anchored-edit.py FILE --old TEXT --new TEXT
"""
import argparse
import contextlib
import fcntl
import os
import shutil
import sys
import tempfile
from pathlib import Path


def apply_edit(text: str, old: str, new: str, allow_multi: bool = False):
    """(result, occurrences) or raise ValueError naming which guard refused."""
    if not old:
        raise ValueError("empty anchor: it would match at every position")
    n = text.count(old)
    if n == 0:
        raise ValueError("anchor absent — nothing was changed; the anchor has "
                         "drifted from the file, which is the failure this refuses")
    if n > 1 and not allow_multi:
        raise ValueError(f"anchor appears {n} times — ambiguous; pass --allow-multi "
                         "to change all of them deliberately")
    if old == new:
        raise ValueError("old == new: the edit is a no-op")
    return (text.replace(old, new) if allow_multi
            else text.replace(old, new, 1)), n


LOCK_SUFFIX = ".anchored-lock"


@contextlib.contextmanager
def _edit_lock(p: Path):
    """Exclusive flock on a sidecar, held across the read AND the replace.

    Locking p itself cannot work: os.replace swaps the inode, so the lock a
    writer holds no longer guards the file its successor sees.
    """
    lock = p.with_name(p.name + LOCK_SUFFIX)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_write(p: Path, text: str, expect: str) -> None:
    """Replace p's contents or leave them untouched — never a truncated middle.

    write_text() truncates the live file first, so an interrupted or short
    write destroys the original. A sibling temp + fsync + os.replace cannot.
    """
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(p, tmp)
        # Second line of defence, for writers that ignore LOCK_SUFFIX. It
        # narrows the window; only the lock closes it. See _edit_lock.
        if p.read_text() != expect:
            raise RuntimeError("target changed since it was read — refusing to overwrite")
        os.replace(tmp, p)          # atomic within a filesystem
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--old"); ap.add_argument("--new")
    ap.add_argument("--old-file"); ap.add_argument("--new-file")
    ap.add_argument("--allow-multi", action="store_true")
    ap.add_argument("--count", type=int, default=None,
                    help="assert the anchor occurs exactly N times before editing")
    a = ap.parse_args(argv)

    old = Path(a.old_file).read_text() if a.old_file else a.old
    new = Path(a.new_file).read_text() if a.new_file else a.new
    if old is None or new is None:
        print("anchored-edit: need --old/--new or --old-file/--new-file", file=sys.stderr)
        return 2

    p = Path(a.file)
    if p.is_symlink():
        # os.replace() swaps the LINK entry, leaving the target untouched and
        # turning the link into a regular file. Refuse rather than retarget.
        print(f"anchored-edit: REFUSED — {p} is a symlink; edit its target directly",
              file=sys.stderr)
        return 2
    if not p.is_file():
        print(f"anchored-edit: no such file: {p}", file=sys.stderr)
        return 2
    with _edit_lock(p):
        return _edit_locked(p, old, new, a)


def _edit_locked(p: Path, old, new, a) -> int:
    """The read-compute-replace critical section. Caller holds the edit lock."""
    before = p.read_text()
    try:
        after, n = apply_edit(before, old, new, a.allow_multi)
    except ValueError as e:
        print(f"anchored-edit: REFUSED — {e}", file=sys.stderr)
        return 2
    if a.count is not None and n != a.count:
        print(f"anchored-edit: REFUSED — anchor occurs {n} times, --count said {a.count}",
              file=sys.stderr)
        return 2
    try:
        _atomic_write(p, after, before)
    except (OSError, RuntimeError) as e:
        # RuntimeError is the changed-target precondition; both leave p untouched.
        print(f"anchored-edit: REFUSED — write not applied, original retained: {e}", file=sys.stderr)
        return 2

    # Must equal `after` EXACTLY: comparing against `before` only proves
    # something changed, which a concurrent writer's content also satisfies.
    reread = p.read_text()
    if reread != after:
        print("anchored-edit: REFUSED — the file on disk is not what this edit computed",
              file=sys.stderr)
        return 2
    print(f"anchored-edit: {p} — anchor matched {n}x, "
          f"replacement present {reread.count(new)}x, {len(before)} -> {len(reread)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
