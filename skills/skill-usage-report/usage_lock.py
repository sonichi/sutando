"""Shared advisory lock for the skill-usage log claim protocol.

ONE module, imported by BOTH sides, because a lock protocol only works if the
writer and the claimer agree exactly — a duplicated helper that drifts is worse
than no lock, since it looks synchronised while it isn't.

The race it closes (#2180 review, reproduced): the hook opens the active log for
append; the reporter renames that log to `.reporting`; the hook's still-open fd
points at the renamed inode and its write lands AFTER the reporter read to EOF
but BEFORE `unlink()`. The record is neither posted nor folded back — the unlink
destroys it, breaking the "events arriving during a report are never lost"
contract.

Protocol:
  * hook     — holds the lock across open + write of the active log.
  * reporter — holds the lock across `log.rename(pending)` (the claim) and across
               the fold-back write, and NOT across the network POST.

That asymmetry is the point. Holding it across the POST would make a
PostToolUse hook wait on a 20s HTTP round trip; holding it only across the
rename means the hook contends with a metadata operation. Either the hook's
append completes before the claim, or it begins after and opens the FRESH active
log — the interleaving that loses a record cannot occur.

The lock lives in its own file, never renamed or unlinked, so both sides always
address the same inode. Locking the log itself would not work: the log is the
thing being renamed out from under them.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import time
from pathlib import Path

LOCK_SUFFIX = ".lock"
RUN_LOCK_SUFFIX = ".runlock"
# Hook-side budget. Deliberately small: the only thing that ever holds this lock
# is a rename or a short append, so a wait beyond this means something is wrong
# and a PostToolUse hook must not keep a tool call waiting to find out.
DEFAULT_TIMEOUT_S = 0.5
POLL_S = 0.01


def lock_path(log: Path) -> Path:
    """Sibling lock file for a given log path. Both sides derive it the same way."""
    return log.with_name(log.name + LOCK_SUFFIX)


@contextlib.contextmanager
def claim_lock(log: Path, timeout: float = DEFAULT_TIMEOUT_S, blocking: bool = False):
    """Yield True if the lock was acquired, False otherwise.

    Yields rather than raising so callers choose their own failure posture: the
    hook drops the record (never block a tool call), the reporter skips the run
    and leaves the log in place (nothing is lost by reporting later).

    `blocking=True` waits the full timeout in small polls instead of a single
    non-blocking attempt. Uses LOCK_NB in a loop rather than a blocking flock so
    the timeout is honoured even where alarm-based timeouts are unavailable.
    """
    p = lock_path(log)
    fh = None
    acquired = False
    try:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = p.open("a+")
        except OSError:
            # Cannot even create the lock file — yield False rather than raise so
            # the caller degrades instead of erroring out of a hook.
            yield False
            return
        deadline = time.monotonic() + (timeout if blocking else 0.0)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(POLL_S)
        yield acquired
    finally:
        if fh is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                fh.close()


def run_lock_path(log: Path) -> Path:
    """Sibling lock file for the reporter-run lock. Distinct from lock_path()."""
    return log.with_name(log.name + RUN_LOCK_SUFFIX)


@contextlib.contextmanager
def reporter_run_lock(log: Path):
    """Exclude a SECOND reporter for the whole duration of a report.

    Why this is a separate lock from claim_lock (#2180 review, second [P1]):

    claim_lock is deliberately released right after `log.rename(pending)` so the
    hook never waits on a 20s HTTP POST. That solved hook-vs-reporter, but left
    reporter-vs-reporter wide open, and the claim FILENAME is not an ownership
    marker:

        A: rename -> pending, release claim lock, begin POST
        B: starts, sees `.reporting` exists, treats it as a CRASHED run,
           folds A's live claim back into the active log, re-POSTs the same
           events, and unlinks the claim
        A: finishes POST, `pending.unlink()` -> FileNotFoundError -> exit 1

    Net: duplicate reports, a destroyed claim, and a reporter that no longer
    always exits 0 — from nothing more exotic than a cron overlapping a manual
    run.

    This lock is held across the ENTIRE reporter run, POST included. That is safe
    precisely because it is a DIFFERENT file from the hook's claim lock: a hook
    contends only for claim_lock, which the reporter still holds for a rename and
    nothing more. Two locks, two lifetimes, one reason each.

    It also disambiguates recovery: while this lock is held, any `.reporting`
    present at startup genuinely IS orphaned, because no other reporter can be
    running. That is what makes the existing crash-recovery branch sound rather
    than a race.

    Non-blocking by design — a second reporter should exit 0 immediately and let
    the next scheduled run do the work, not queue up behind a network call.
    """
    p = run_lock_path(log)
    fh = None
    acquired = False
    try:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = p.open("a+")
        except OSError:
            yield False
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        yield acquired
    finally:
        if fh is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                fh.close()
