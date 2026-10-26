#!/usr/bin/env python3
"""One owner of a live task's archive-then-report transition: claim exactly the planned file generation, place it without clobbering, then publish a notice naming where it landed — journaled stage by stage, so a crash at any point is recovered once.

A task file is a record another writer may replace, and a notice saying a task
is archived is a promise. `transition()` keeps it as a staged transaction:

  Pre-flight. `tasks/` and `tasks/archive/` are opened once as directory
  handles (no symlink followed; the archive dir is created when missing) and
  stay open through the pass and any recovery, so a directory swapped for a
  symlink afterwards is refused, never followed. Every name is bare and every
  source is a regular file still at the generation the plan captured
  (`generation_of`). Any failure means nothing moves and nothing is published.
  One pass at a time holds `.transition.lock` in the archive dir; under it, a
  caller's approved `recovery_effects()` must still match exactly, or nothing runs.

  Journal. A journal in the archive dir (`.notice-<second>-<pid>-<token>.json`,
  beside a flock'd `.lock` the owner holds while alive) records the stage:
  `planned` (the moves), then per file its private claim name BEFORE the
  rename to it (`claiming`, with the notice pre-rendered as if it lands),
  then where it landed or that it was quarantined, with the notice rendered
  for exactly the recorded landings, then `publishing` with the reserved
  notice name. It is
  removed when the notice is committed. A recorded claim is in progress until
  the journal says otherwise: never read as "gone".

  Per file (`task_archive.claim_generation` / `place_claim`): the live name is
  taken by one atomic rename into `<name>.claim-<pid>-<token>` in the same
  directory (same filesystem, never a copy), judged through its own descriptor
  against the planned generation and by the caller's `still_safe` (asked
  before the rename and again while this pass holds the bytes), and only a
  match is placed by the no-clobber move through the handles (`.N` on a taken
  name; the archive dir is fsynced before the claim is unlinked, and a failure
  after the link leaves the landing recorded as unsynced, never given back).
  A mismatched claim is linked
  back to its live name without replacing; if a writer already recreated that
  name, the claimed bytes stay quarantined under the claim name, recorded, and
  reported. No task-bearing name is ever unlinked: every removal is
  `task_archive.retire`, a rename into `tasks/archive/.retired/` judged by its
  full generation, and anything unexpected it set aside keeps the journal.

  Notice. Rendered from the FINAL landed names, published last at the reserved
  name through `result_publish.publish_witnessed`: a dot-named witness link of
  the same inode stays in results/ until the journal is gone, so a consumer
  that renamed the result away still leaves proof of the commit. A body over
  PROACTIVE_MAX_BODY_B is refused: the delivery adapter dead-letters it.

  Recovery. The next pass takes each journal whose lock is free (its owner is
  gone) and continues it from the recorded stage: a claim already linked into
  the archive is only unlinked, an unplaced claim is placed, a placement whose
  record was lost is found by generation among every `.N`. The notice names
  only what landed and ends with `(ref <txn>)`, the journal's transaction id;
  a file at the reserved name, or a witness with a second link, counts as
  published only when it carries that id, so another notice is never adopted
  and a retired one is never sent again. A claim is unlinked only when it is
  the same inode as its landing. While any claimed file cannot be found or
  is ambiguous, nothing is published and the journal is kept. Each generation ends with
  exactly one copy, live or archived; a second recovery pass does nothing new.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Callable

import result_publish
import task_archive

__all__ = ["PROACTIVE_MAX_BODY_B", "JOURNAL_PREFIX", "TransitionRefused", "generation_of",
           "is_bare_name", "preflight", "publish_exclusive", "archive_generation", "transition",
           "pending_notices"]

# Mirrors ag2_sparrow.remote_gateway_bridge._PROACTIVE_MAX_BODY_B; a test pins the two.
PROACTIVE_MAX_BODY_B = 48 * 1024
JOURNAL_PREFIX = ".notice-"
_NAME_ATTEMPTS = 16
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
Render = Callable[[dict], "str | None"]
StillSafe = Callable[[str, str], "str | None"]
Arbitrate = Callable[[str], "contextlib.AbstractContextManager"]


class TransitionRefused(Exception):
    """A precondition failed; nothing was changed for the item it names."""


def _checkpoint(point: str, name: str) -> None:
    """A named injection point for tests (`before_claim`, `after_claim`); a no-op in production."""


def is_bare_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and Path(name).name == name


_generation = task_archive.file_generation


def generation_of(path: Path) -> str | None:
    """The identity of the bytes at `path` right now, or None when absent.

    Device, inode, size and mtime_ns, never following a symlink: a replaced
    file is a new generation even when it carries the same name, which is how
    the watcher tells a re-emitted task from a rewritten one."""
    try:
        return _generation(os.lstat(path))
    except OSError:
        return None


def _source_problem(tasks_fd: int, name: str, generation: str | None) -> str | None:
    if not is_bare_name(name):
        return "not a bare filename"
    if not generation:
        return "no generation was captured when it was planned"
    try:
        st = os.lstat(name, dir_fd=tasks_fd)
    except FileNotFoundError:
        return "no longer in tasks/"
    if stat.S_ISLNK(st.st_mode):
        return "a symlink, not a task file"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if _generation(st) != generation:
        return "changed since it was planned (a new generation is live under this name)"
    return None


# ── the pinned directory handles ─────────────────────────────────────────────

class _Dirs:
    """The two directory handles a pass works through, and their identity at pre-flight."""

    def __init__(self, tasks_dir: Path, archive_dir: Path):
        self.tasks_dir, self.archive_dir = tasks_dir, archive_dir
        self.tasks_fd = os.open(tasks_dir, _DIR_FLAGS)
        try:
            try:
                os.mkdir(archive_dir.name, dir_fd=self.tasks_fd)
            except FileExistsError:
                pass
            self.archive_fd = os.open(archive_dir.name, _DIR_FLAGS, dir_fd=self.tasks_fd)
        except BaseException:
            os.close(self.tasks_fd)
            raise

    def close(self) -> None:
        os.close(self.archive_fd)
        os.close(self.tasks_fd)

    def problem(self) -> str | None:
        """Why the names no longer lead to the pinned directories, or None."""
        for label, fd, path, dir_fd in (("tasks/", self.tasks_fd, str(self.tasks_dir), None),
                                        ("tasks/archive/", self.archive_fd, self.archive_dir.name,
                                         self.tasks_fd)):
            try:
                now = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                return f"{label} is gone since pre-flight ({exc})"
            pinned = os.fstat(fd)
            if (now.st_dev, now.st_ino) != (pinned.st_dev, pinned.st_ino) or pinned.st_nlink == 0:
                return f"{label} was replaced since pre-flight"
        return None


def _open_dirs(tasks_dir: Path, archive_dir: Path) -> "tuple[_Dirs | None, str | None]":
    if archive_dir.parent != tasks_dir:
        return None, f"refused: {archive_dir} is not directly under {tasks_dir}"
    try:
        return _Dirs(tasks_dir, archive_dir), None
    except OSError as exc:
        try:
            mode = os.lstat(archive_dir).st_mode
        except OSError:
            return None, f"could not open {tasks_dir} or create {archive_dir}: {exc}"
        if stat.S_ISLNK(mode):
            return None, f"refused: {archive_dir} is a symlink"
        if not stat.S_ISDIR(mode):
            return None, f"could not create {archive_dir}: a file holds the name"
        return None, f"could not open {archive_dir}: {exc}"


def preflight(tasks_dir: Path, archive_dir: Path, moves: dict[str, str | None]) -> list[str]:
    """Every reason the moves could not be performed ({name: generation}), one
    line each; an empty list means they may be promised. Creates `archive_dir`
    when it is missing."""
    if not moves:
        return []
    dirs, problem = _open_dirs(tasks_dir, archive_dir)
    if dirs is None:
        return [problem or "could not open the directories"]
    try:
        return _problems(dirs, moves)
    finally:
        dirs.close()


def _problems(dirs: _Dirs, moves: dict) -> list[str]:
    return [f"refused to move {n!r}: {p}"
            for n, p in ((n, _source_problem(dirs.tasks_fd, n, g)) for n, g in moves.items()) if p]


# ── publication ──────────────────────────────────────────────────────────────

def _mint_name(prefix: str, now: float | None) -> str:
    second = int(time.time() if now is None else now)
    return f"{prefix}{second}-{os.getpid()}-{secrets.token_hex(4)}.txt"


def _stamp(body: str, txn: str) -> str:
    """The notice as published: its last line names the transaction that owns it."""
    return f"{body.rstrip()}\n\n(ref {txn})\n"


def _owned_by(held: bytes, txn: str) -> bool:
    return held.rstrip().endswith(f"(ref {txn})".encode())


def _witness(name: str, txn: str) -> str:
    return f".{name}.{txn}.witness"


def _fsync_path(directory: Path) -> None:
    """A barrier on a directory named by path (results/ is not one of the pinned handles)."""
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _drop_witness(results_dir: Path, name: "str | None", txn: "str | None") -> None:
    """Only after the journal's removal is durable (`_Journal.remove`): a witness
    gone while the journal survives a power loss would read as "never committed"."""
    if name and txn:
        try:
            os.unlink(results_dir / _witness(name, txn))
            _fsync_path(results_dir)
        except OSError:
            pass  # a witness that outlives its journal is swept by a later pass


def _publish_at(results_dir: Path, name: str, body: str, txn: "str | None" = None) -> Path:
    """Publish `body` at exactly `results_dir/name`, whole, never replacing; with
    `txn`, a witness link of the same inode stays beside it until the journal goes.
    Raises TransitionRefused for a body the adapter could never deliver,
    FileExistsError when the name is already committed; OSError propagates."""
    size = len(body.encode("utf-8"))
    if size > PROACTIVE_MAX_BODY_B:
        raise TransitionRefused(f"body is {size} bytes (> {PROACTIVE_MAX_BODY_B}); undeliverable")
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:  # a file holds the directory's name: not a taken result name
        raise NotADirectoryError(errno.ENOTDIR, f"{results_dir} is not a directory") from exc
    if txn:
        return result_publish.publish_witnessed(results_dir / name, body, results_dir / _witness(name, txn))
    return result_publish.publish_text(results_dir / name, body, exclusive=True)


def publish_exclusive(results_dir: Path, prefix: str, body: str,
                      now: float | None = None) -> Path:
    """Publish `body` as `<prefix><second>-<pid>-<token>.txt` in `results_dir`,
    whole and under a name this call alone owns."""
    for _ in range(_NAME_ATTEMPTS):
        try:
            return _publish_at(results_dir, _mint_name(prefix, now), body)
        except FileExistsError:
            continue  # the one publisher refused the taken name; mint another
    raise OSError(f"could not reserve a result name under {results_dir}")


# ── the journal ──────────────────────────────────────────────────────────────

class _Journal:
    """One pass's record in the archive dir: `<name>.json` rewritten atomically
    at every stage, and `<name>.lock` flock'd by the owner while it lives."""

    def __init__(self, dirs: _Dirs, name: str, data: dict, lock_fd: int):
        self.dirs, self.name, self.data, self.lock_fd = dirs, name, data, lock_fd

    @classmethod
    def create(cls, dirs: _Dirs, now: float | None, data: dict) -> "_Journal":
        second = int(time.time() if now is None else now)
        name = f"{JOURNAL_PREFIX}{second}-{os.getpid()}-{secrets.token_hex(6)}.json"
        lock_fd = os.open(f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644,
                          dir_fd=dirs.archive_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        j = cls(dirs, name, data, lock_fd)
        try:
            j.persist()
        except BaseException:
            j._drop_lock()
            raise
        return j

    @classmethod
    def take(cls, dirs: _Dirs, name: str) -> "_Journal | None":
        """The journal `name` if its owner is gone (the lock is free); None
        while a live pass holds it. Unreadable data reads as {}."""
        try:
            lock_fd = os.open(f"{name}.lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o644,
                              dir_fd=dirs.archive_fd)
        except OSError:
            return None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            return None
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dirs.archive_fd)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        return cls(dirs, name, data, lock_fd)

    def persist(self) -> None:
        tmp = f"{self.name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=self.dirs.archive_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp, self.name, src_dir_fd=self.dirs.archive_fd, dst_dir_fd=self.dirs.archive_fd)
            task_archive._fsync_dir(self.dirs.archive_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=self.dirs.archive_fd)
            except OSError:
                pass
            raise

    def set(self, **fields) -> None:
        self.data.update(fields)
        self.persist()

    def _drop_lock(self) -> None:
        try:
            os.unlink(f"{self.name}.lock", dir_fd=self.dirs.archive_fd)
        except OSError:
            pass
        self._close()

    def _close(self) -> None:
        if self.lock_fd >= 0:
            os.close(self.lock_fd)
            self.lock_fd = -1

    def remove(self) -> None:
        """Unlink the journal and make that durable; OSError when the barrier failed."""
        try:
            try:
                os.unlink(self.name, dir_fd=self.dirs.archive_fd)
            except FileNotFoundError:
                pass
            task_archive._fsync_dir(self.dirs.archive_fd)
        finally:
            self._drop_lock()

    def release(self) -> None:
        """Leave the journal for recovery; only the owner's hold on the lock goes."""
        self._close()


def pending_notices(archive_dir: Path) -> list[str]:
    """Journals an interrupted pass left behind, read-only, for the plan."""
    try:
        return sorted(n for n in os.listdir(archive_dir)
                      if n.startswith(JOURNAL_PREFIX) and n.endswith(".json"))
    except OSError:
        return []


# ── the per-file move ────────────────────────────────────────────────────────

class _ClaimRefused(TransitionRefused):
    """Refused after the rename: given back, kept as `kept` when its name was retaken,
    or `unresolved` when the give-back failed (the journaled refusal finishes it)."""

    def __init__(self, message: str, kept: "str | None", unresolved: bool = False):
        super().__init__(message)
        self.kept, self.unresolved = kept, unresolved


def _claim(dirs: _Dirs, name: str, generation: str | None, claim: str | None = None,
           still_safe: "StillSafe | None" = None, arbitrate: "Arbitrate | None" = None,
           on_refuse: "Callable[[str], None] | None" = None) -> str:
    with arbitrate(name) if arbitrate else contextlib.nullcontext():
        return _claim_held(dirs, name, generation, claim, still_safe, on_refuse)


def _claim_held(dirs, name, generation, claim, still_safe, on_refuse) -> str:
    problem = dirs.problem() or _source_problem(dirs.tasks_fd, name, generation)
    problem = problem or (still_safe(name, name) if still_safe else None)
    if problem:
        raise TransitionRefused(f"refused to move {name!r}: {problem}")
    _checkpoint("before_claim", name)
    try:
        claim = task_archive.claim_generation(name, src_dir_fd=dirs.tasks_fd, claim=claim)
    except FileNotFoundError:
        raise TransitionRefused(f"refused to move {name!r}: no longer in tasks/ — another pass "
                                "archived it and sent its notice") from None
    _checkpoint("after_claim", name)
    problem = still_safe(name, claim) if still_safe else None
    if not problem:
        return claim
    if on_refuse:  # recorded first; if that fails, the durable "unjudged" mark still returns it
        try:
            on_refuse(claim)
        except OSError:
            pass
    try:
        back = task_archive.give_back(claim, name, problem, dirs.tasks_fd, archive_fd=dirs.archive_fd,
                                      expect=generation)
    except OSError as exc:
        raise _ClaimRefused(f"refused to move {name!r}: {problem}; giving it back failed ({exc}), "
                            "so the refusal is journaled and the next pass returns it",
                            None, unresolved=True) from exc
    where = (f"its name was taken again, so it is quarantined as tasks/{back.kept_as} — rename "
             "it back to requeue" if back.kept_as else "it is back under its name, live")
    raise _ClaimRefused(f"refused to move {name!r}: {problem}; {where}", back.kept_as)


def _place(dirs: _Dirs, claim: str, name: str,
           generation: str) -> "tuple[str | None, str | None, bool, str | None, bool]":
    """(landed name, error line, quarantined, unsynced landing, unresolved): one of
    the first two is set; `quarantined` says the claim stayed in tasks/ on purpose;
    `unsynced` names a landing whose durability is unknown; `unresolved` says the
    journal must be kept for the next pass."""
    try:
        landed = task_archive.place_claim(claim, name, generation, name, src_dir_fd=dirs.tasks_fd,
                                          dst_dir_fd=dirs.archive_fd)
        return landed.name, None, False, None, False
    except task_archive.PlacementUnsynced as exc:
        return None, (f"{name!r} is linked as tasks/archive/{exc.landed} but not yet durable "
                      f"({exc.cause}); its journal is kept and the next pass finishes it"), \
            False, exc.landed, True
    except task_archive.PlacementMismatch as exc:
        return None, _mismatch_line(name, claim, getattr(exc, "kept_as", None)), False, None, \
            {"claim": claim, "displaced": getattr(exc, "kept_as", None)}
    except task_archive.PlacementSourceReplaced as exc:
        return None, _mismatch_line(name, exc.kept_as, None, exc.landed), False, None, \
            {"claim": exc.kept_as, "displaced": None, "landed": exc.landed}
    except task_archive.GenerationChanged as exc:
        if exc.kept_as:
            return None, (f"refused to move {name!r}: {exc.why}; its name was taken again, so it is "
                          f"quarantined as tasks/{exc.kept_as} — rename it back to requeue"), True, None, False
        return None, (f"refused to move {name!r}: {exc.why}; it is back under its name, live, and "
                      "the next pass reports it"), False, None, False


def _mismatch_line(name: str, claim: str, displaced: "str | None", landed: "str | None" = None) -> str:
    if landed:
        return (f"refused to finish {name!r}: its planned bytes are archived as tasks/archive/{landed}, "
                f"but its claim name was replaced meanwhile and that file is kept as tasks/{claim}; "
                "both stay where they are and the journal waits for the operator")
    where = (f"the planned bytes are kept as tasks/{displaced}" if displaced
             else "the planned bytes could not be kept")
    return (f"refused to move {name!r}: tasks/{claim} was replaced after it was judged, so nothing "
            f"was archived; {where}; both stay where they are and the journal waits for the operator")


def _record_mismatch(dirs: _Dirs, j: "_Journal", name: str, pending) -> None:
    """A replaced claim is never given back or cleared: marked, and journaled for the operator."""
    if isinstance(pending, dict):
        j.data.setdefault("mismatched", {})[name] = pending
        if "/" not in pending["claim"]:  # a name still in tasks/, not one already retired
            _mark_quarantine(dirs, pending["claim"])


def archive_generation(tasks_dir: Path, name: str, generation: str | None,
                       archive_dir: Path) -> Path:
    """Move `tasks_dir/name` into `archive_dir` if it is still `generation`;
    returns where it landed. Raises TransitionRefused when a precondition
    fails (nothing changed, or the file is back under its name or quarantined)."""
    dirs, problem = _open_dirs(tasks_dir, archive_dir)
    if dirs is None:
        raise TransitionRefused(problem or "could not open the directories")
    try:
        claim = _claim(dirs, name, generation)
        landed, error, _, _, _ = _place(dirs, claim, name, generation or "")
        if error:
            raise TransitionRefused(error)
        return archive_dir / landed
    finally:
        dirs.close()


# ── recovery of an interrupted pass ──────────────────────────────────────────

def _find_placed(dirs: _Dirs, name: str, generation: str) -> str | None:
    return task_archive.find_landed(name, generation, dst_dir_fd=dirs.archive_fd)


QUARANTINE_SUFFIX = ".quarantined"


def _mark_quarantine(dirs: _Dirs, claim: str) -> None:
    """A quarantined claim is kept on purpose; the marker keeps the sweep off it."""
    try:
        fd = os.open(claim + QUARANTINE_SUFFIX, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=dirs.tasks_fd)
        os.close(fd)
    except OSError:
        pass


_CLAIM_RE = re.compile(r"^(?P<live>.+)\.claim-(?P<pid>\d+)-[0-9a-f]+$")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False  # gone, or a number no process can carry
    except PermissionError:
        return True
    return True


def _sweepable(tasks_fd: int, journaled: set[str]) -> dict[str, str]:
    """The claims `_sweep_dead_claims` acts on — unjournaled, pid gone, not kept on
    purpose — each with its generation, which is what an approval binds."""
    found = {}
    for entry in sorted(os.listdir(tasks_fd)):
        m = _CLAIM_RE.match(entry)
        if not m or entry in journaled or _pid_alive(int(m.group("pid"))):
            continue
        if _exists(entry + QUARANTINE_SUFFIX, tasks_fd):
            continue
        try:
            found[entry] = _generation(os.lstat(entry, dir_fd=tasks_fd))
        except OSError:
            continue
    return found


def _sweep_dead_claims(dirs: _Dirs, journaled: set[str], out: dict,
                       only: "dict[str, str] | None" = None) -> None:
    """A `<name>.claim-<pid>-…` in tasks/ that no journal names and whose pid is
    gone was left by a pass that died between its claim and its record (or by a
    journal since removed): it is given back to its live name without replacing,
    for the next plan to judge; when that name is taken it stays, reported.
    Only the generation seen (or, with `only`, approved) is ever moved."""
    for entry, generation in _sweepable(dirs.tasks_fd, journaled).items():
        if only is not None and only.get(entry) != generation:
            continue
        live = _CLAIM_RE.match(entry).group("live")
        try:
            os.link(entry, live, src_dir_fd=dirs.tasks_fd, dst_dir_fd=dirs.tasks_fd)
        except FileExistsError:
            out["errors"].append(f"orphaned claim tasks/{entry} from a dead pass; its name {live!r} is "
                                 "taken, so it is kept — rename it back to requeue")
            continue
        except OSError as exc:
            out["errors"].append(f"orphaned claim tasks/{entry} from a dead pass could not be given back ({exc})")
            continue
        with task_archive.retired_dir(dirs.archive_fd) as rfd:
            if _generation(os.lstat(live, dir_fd=dirs.tasks_fd)) != generation:  # rewritten or replaced
                moved = task_archive.retire(live, src_dir_fd=dirs.tasks_fd, retired_fd=rfd, expect=None)
                out["errors"].append(f"orphaned claim tasks/{entry} changed since it was approved; it is "
                                     f"not made live (that link is kept as {_retired(moved)})")
                continue
            stray = task_archive.retire(entry, src_dir_fd=dirs.tasks_fd, retired_fd=rfd, expect=generation)
            if stray and _same_file(stray, rfd, live, dirs.tasks_fd):  # rewritten in place: live shares it
                moved = task_archive.retire(live, src_dir_fd=dirs.tasks_fd, retired_fd=rfd, expect=None)
                out["errors"].append(f"orphaned claim tasks/{entry} was rewritten while it was given back; "
                                     f"it is not made live (kept as {_retired(stray)} and {_retired(moved)})")
                continue
        if stray:
            out["errors"].append(f"orphaned claim tasks/{entry} was given back as {live!r}, but a file "
                                 f"that replaced it is kept as {_retired(stray)}")
        out["swept"].append({"claim": entry, "live": live})


def _retired(name: str) -> str:
    return f"tasks/archive/{task_archive.RETIRED_DIR}/{name}"


def _retired_strays(dirs: _Dirs, claim: str, generation: str) -> "list[str]":
    """Files `retire` set aside under this claim's name that are not its planned bytes."""
    try:
        with task_archive.retired_dir(dirs.archive_fd) as rfd:
            return [n for n in task_archive.retired_names(claim, rfd)
                    if _generation(os.lstat(n, dir_fd=rfd)) != generation]
    except FileNotFoundError:
        return []


def _exists(name: str, dir_fd: int | None = None) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
        return True
    except OSError:
        return False


def _recover(dirs: _Dirs, j: _Journal, results_dir: Path, out: dict,
             render_recovered: "Render | None" = None) -> None:
    d = j.data
    report = {"journal": j.name, "landed": {}, "result": None, "errors": []}
    out["recovered"].append(report)
    problem = dirs.problem()
    if problem:
        report["errors"].append(f"recovery of tasks/archive/{j.name} deferred: {problem}")
    elif not isinstance(d.get("moves"), dict) or not isinstance(d.get("body"), (str, type(None))):
        report["errors"].append(f"journal tasks/archive/{j.name} is unreadable; left for the operator")
    if report["errors"]:
        out["errors"] += report["errors"]
        j.release()
        return
    try:
        _continue(dirs, j, results_dir, report, render_recovered)
    except OSError as exc:
        report["errors"].append(f"recovery of tasks/archive/{j.name} interrupted ({exc}); retried next pass")
        j.release()
    out["errors"] += report["errors"]


def _same_file(a: str, a_fd: int, b: str, b_fd: int) -> bool:
    try:
        sa, sb = os.lstat(a, dir_fd=a_fd), os.lstat(b, dir_fd=b_fd)
    except OSError:
        return False
    return stat.S_ISREG(sa.st_mode) and (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _finish_return(dirs: _Dirs, name: str, claim: str, generation: str) -> "str | None":
    """Complete a journaled refusal: the claim goes back to its live name without
    replacing, is never placed, and its name is retired, never unlinked. Returns
    an error line, or None once returned."""
    has_claim, has_live = _exists(claim, dirs.tasks_fd), _exists(name, dirs.tasks_fd)
    if has_claim and has_live and not _same_file(claim, dirs.tasks_fd, name, dirs.tasks_fd):
        _mark_quarantine(dirs, claim)
        return (f"refused to move {name!r}: its name was taken again while it was being given "
                f"back, so it is quarantined as tasks/{claim} — rename it back to requeue")
    if not has_claim and not has_live:
        return (f"lost track of {name!r}: it was refused and being given back, but neither "
                f"tasks/{name} nor tasks/{claim} exists — check tasks/archive/")
    if not has_claim:  # retired by a pass that died: a replacement it set aside is still the operator's
        strays = _retired_strays(dirs, claim, generation)
        if strays:
            return (f"lost track of {name!r}: it is back under its name, but a file that replaced "
                    f"its claim is kept as {', '.join(_retired(x) for x in strays)}")
        return None
    with task_archive.retired_dir(dirs.archive_fd) as rfd:
        if not has_live:
            os.link(claim, name, src_dir_fd=dirs.tasks_fd, dst_dir_fd=dirs.tasks_fd)
            task_archive._fsync_dir(dirs.tasks_fd)
            if _generation(os.lstat(name, dir_fd=dirs.tasks_fd)) != generation:
                moved = task_archive.retire(name, src_dir_fd=dirs.tasks_fd, retired_fd=rfd, expect=None)
                return (f"lost track of {name!r}: its claim tasks/{claim} no longer holds the planned "
                        f"bytes; that file is kept as {_retired(moved)} and the claim stays")
        stray = task_archive.retire(claim, src_dir_fd=dirs.tasks_fd, retired_fd=rfd, expect=generation)
    if stray:
        return (f"lost track of {name!r}: it is back under its name, but a file that replaced its "
                f"claim is kept as {_retired(stray)}")
    return None


class _LandingChanged(OSError):
    """An archived landing changed between its check and its notice."""


def _read_landing(dirs: _Dirs, where: str) -> "tuple[str, bytes, float] | None":
    """(generation, bytes, mtime) of a landing read through one descriptor that follows
    no symlink; None when it is gone."""
    if not _NOFOLLOW:  # no no-follow primitive: a link could not be refused
        return None
    try:
        fd = os.open(where, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=dirs.archive_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None
        data = fh.read()
        try:  # still this file under this name once read
            after, named = os.fstat(fh.fileno()), os.lstat(where, dir_fd=dirs.archive_fd)
        except OSError:
            return None
        if _generation(after) != _generation(st) or _generation(named) != _generation(st):
            return None
        return _generation(st), data, st.st_mtime


def _barriers(dirs: _Dirs) -> None:
    """Both directories durable before a found landing is accepted: a crash may
    have left the archive link or the claim's removal unsynced."""
    task_archive._fsync_dir(dirs.archive_fd)
    task_archive._fsync_dir(dirs.tasks_fd)


def _continue(dirs: _Dirs, j: _Journal, results_dir: Path, report: dict,
              render_recovered: "Render | None" = None) -> None:
    d = j.data
    claims, landed = dict(d.get("claims") or {}), dict(d.get("landed") or {})
    quarantined, refused = dict(d.get("quarantined") or {}), dict(d.get("refused") or {})
    unresolved = False
    for name, generation in d["moves"].items():
        if name in landed or name in quarantined:
            continue
        held = (d.get("mismatched") or {}).get(name)
        if held:
            unresolved = True
            report["errors"].append(_mismatch_line(name, held.get("claim"), held.get("displaced"),
                                                   held.get("landed")))
            continue
        unjudged = dict(d.get("unjudged") or {})
        if name in refused or name in unjudged:  # refused, or never durably judged safe: never placed
            held_claim = refused.get(name) or unjudged[name]
            error = _finish_return(dirs, name, held_claim, generation)
            if error is None or "quarantined" in error:
                quarantined.update({name: held_claim} if error else {})
                claims.pop(name, None)
                refused.pop(name, None)
                unjudged.pop(name, None)
                d["unjudged"] = unjudged
            if error:
                report["errors"].append(error)
                unresolved = unresolved or "lost track" in error
            j.set(claims=claims, quarantined=quarantined, refused=refused)
            continue
        if not claims.get(name):
            continue  # never claimed: still live under its name, planned afresh
        claim = claims[name]
        found = _find_placed(dirs, name, generation)
        if found and _exists(claim, dirs.tasks_fd):
            if not _same_file(claim, dirs.tasks_fd, found, dirs.archive_fd):
                unresolved = True
                report["errors"].append(
                    f"{name!r} is archived as tasks/archive/{found}, but tasks/{claim} now holds a "
                    f"different file; both are kept, and the journal tasks/archive/{j.name} waits "
                    "for the operator")
                continue
            task_archive._fsync_dir(dirs.archive_fd)  # the landing is durable before the claim goes
            with task_archive.retired_dir(dirs.archive_fd) as rfd:  # linked, then killed before the retire
                stray = task_archive.retire(claim, src_dir_fd=dirs.tasks_fd, retired_fd=rfd, expect=generation)
            _barriers(dirs)
            if stray:
                unresolved = True
                report["errors"].append(f"{name!r} is archived as tasks/archive/{found}, but a file that "
                                        f"replaced its claim is kept as {_retired(stray)}; the journal waits "
                                        "for the operator")
                continue
            landed[name] = found
        elif _exists(claim, dirs.tasks_fd):
            got, error, kept, _, pending = _place(dirs, claim, name, generation)  # renamed, unplaced
            if got:
                landed[name] = got
            else:
                report["errors"].append(error)
                unresolved = unresolved or bool(pending)
                _record_mismatch(dirs, j, name, pending)
                if kept:
                    quarantined[name] = claim
                    _mark_quarantine(dirs, claim)
        elif _source_problem(dirs.tasks_fd, name, generation) is None:
            del claims[name]  # recorded but never renamed: still live under its name, planned afresh
        elif found:
            _barriers(dirs)  # renamed and placed; the crash hid the record or a barrier failed
            strays = _retired_strays(dirs, claim, generation)
            if strays:  # the claim name held another file when it was retired
                unresolved = True
                report["errors"].append(f"{name!r} is archived as tasks/archive/{found}, but "
                                        f"{', '.join(_retired(x) for x in strays)} holds a file that "
                                        "replaced its claim; the journal waits for the operator")
                continue
            landed[name] = found
        else:
            unresolved = True
            report["errors"].append(f"lost track of {name!r}: its claim {claim} is gone and no "
                                    "archived copy carries its generation; its journal "
                                    f"tasks/archive/{j.name} and its notice wait until it is found "
                                    "— check tasks/archive/")
        j.set(claims=claims, landed=landed, quarantined=quarantined)
    report["landed"] = landed
    contents = {}  # the notice describes these bytes: read once, bound to the planned generation
    for name, where in sorted(landed.items()):
        got = _read_landing(dirs, where)
        if got is None or got[0] != d["moves"].get(name):
            unresolved = True
            report["errors"].append(f"{name!r} was archived as tasks/archive/{where}, but that file is "
                                    "now missing or another generation; the journal waits for the operator")
        else:
            contents[name] = got[1:]
    if unresolved:  # a notice now would omit it, and a later pass would adopt that notice
        j.release()
        return
    if landed or d.get("result"):
        bound = None
        if render_recovered is not None:
            def bound(now_landed, contents=contents):
                body = render_recovered(now_landed, contents)
                for name, where in now_landed.items():  # still the bytes just rendered
                    got = _read_landing(dirs, where)
                    if got is None or got[0] != d["moves"].get(name):
                        raise _LandingChanged(f"{name!r}: tasks/archive/{where} changed while its notice "
                                              "was rendered; the journal waits for the operator")
                return body
        if not _publish_recovered(j, results_dir, landed, report, bound):
            return
    j.remove()
    _drop_witness(results_dir, d.get("result"), d.get("txn"))


def _recovered_body(d: dict, landed: dict) -> "str | None":
    """The notice for exactly what landed: the body rendered for the recorded
    landings, or the one pre-rendered for the claim in flight when it landed too."""
    body, done = d.get("body"), d.get("rendered_for") or {}
    if set(landed) != set(done) and set(landed) == set(d.get("next_for") or {}):
        body, done = d.get("next_body"), d.get("next_for") or {}
    if set(landed) != set(done) or body is None:
        return None
    renamed = {k: v for k, v in landed.items() if done.get(k) != v}
    note = "".join(f"\n{k} is archived as tasks/archive/{v}." for k, v in sorted(renamed.items()))
    return f"{body.rstrip()}\n\n(Published by a later pass after an interrupted one.){note}\n"


def _committed(d: dict, results_dir: Path) -> bool:
    """The reserved notice is already published (witnessed, or held at its name)."""
    txn, name = d.get("txn"), d.get("result")
    if d.get("stage") != "publishing" or not isinstance(txn, str) or not txn or not name:
        return False
    if _witnessed(results_dir / _witness(name, txn), txn):
        return True
    try:
        return _owned_by((results_dir / name).read_bytes(), txn)
    except FileNotFoundError:
        return False


def _publish_recovered(j: _Journal, results_dir: Path, landed: dict, report: dict,
                       render_recovered: "Render | None" = None) -> bool:
    """Publish the journal's notice once. A taken name counts as this notice only
    when the file carries this journal's transaction id; any other gets a new name.
    With `render_recovered`, an unpublished notice is rendered afresh from what
    landed, by the caller's current rules, instead of from the journal."""
    d = j.data
    txn = d.get("txn")
    if render_recovered is not None and not _committed(d, results_dir):
        body = render_recovered(dict(landed))  # bound to the bytes read and re-checked after
        if body is None:
            return True  # nothing landed is still the owner's to hear about
        txn = txn if isinstance(txn, str) and txn else secrets.token_hex(8)
        body = f"{body.rstrip()}\n\n(Published by a later pass after an interrupted one.)\n"
        j.set(stage="publishing", txn=txn, body=_stamp(body, txn),
              result=d.get("result") or _mint_name(d.get("prefix") or "proactive-", None))
    if (d.get("stage") != "publishing" or not isinstance(d.get("body"), str) or not d.get("result")
            or not isinstance(txn, str) or not txn):
        body = _recovered_body(d, landed)
        if body is None:
            return True
        txn = txn if isinstance(txn, str) and txn else secrets.token_hex(8)
        j.set(stage="publishing", txn=txn, body=_stamp(body, txn),
              result=_mint_name(d.get("prefix") or "proactive-", None))
    body = d["body"]
    for _ in range(_NAME_ATTEMPTS):
        target = results_dir / d["result"]
        if _witnessed(results_dir / _witness(d["result"], txn), txn):
            _fsync_path(results_dir)  # visible is not durable: the commit may be unsynced
            report["result"] = str(target)  # committed; its consumer may have retired it since
            return True
        try:
            held = target.read_bytes()
        except FileNotFoundError:
            held = None
        except OSError as exc:
            held = exc
        if isinstance(held, bytes) and _owned_by(held, txn):
            _fsync_path(results_dir)
            report["result"] = str(target)  # committed before the crash: no second notice
            return True
        if held is None:
            try:
                report["result"] = str(_publish_at(results_dir, d["result"], body, txn))
                return True
            except FileExistsError:
                continue  # taken meanwhile: judged by its bytes on the next turn
            except (OSError, TransitionRefused) as exc:
                held = exc
        if isinstance(held, BaseException):
            report["errors"].append(f"could not publish tasks/archive/{j.name} ({held}); retried next pass")
            j.release()
            return False
        j.set(result=_mint_name(d.get("prefix") or "proactive-", None))  # another notice's name
    report["errors"].append(f"could not reserve a result name for tasks/archive/{j.name}; retried next pass")
    j.release()
    return False


def _witnessed(witness: Path, txn: str) -> bool:
    """The commit happened: the witness shares its inode with a second name,
    wherever a consumer has renamed that name since. Only a definitely absent
    witness reads as False; any other read failure raises, keeping the journal."""
    try:
        st = os.lstat(witness)
    except (FileNotFoundError, NotADirectoryError):  # it cannot exist
        return False
    return st.st_nlink >= 2 and _owned_by(witness.read_bytes(), txn)


def _sweep_witnesses(results_dir: Path, txns: set[str], dirs_archive_fd: int) -> None:
    """A witness outlives its journal only after a crash between the two removals.
    The archive dir is fsynced first, so no removed journal can come back after it."""
    try:
        names = [n for n in os.listdir(results_dir)
                 if n.startswith(".") and n.endswith(".witness") and n.rsplit(".", 2)[-2] not in txns]
    except OSError:
        return
    if not names:
        return
    try:
        task_archive._fsync_dir(dirs_archive_fd)
    except OSError:
        return
    for n in names:
        try:
            os.unlink(results_dir / n)
        except OSError:
            pass
    try:
        _fsync_path(results_dir)
    except OSError:
        pass


def _journal_claims(archive_fd: int, name: str) -> "tuple[dict, str] | None":
    """A journal's (data, sha256 of its bytes), or None when it cannot be read."""
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=archive_fd)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return (data if isinstance(data, dict) else {}), hashlib.sha256(raw).hexdigest()


def _effects(dirs: _Dirs) -> dict:
    """What recovery would act on right now: each journal by its bytes, and each
    dead pass's claim the sweep would give back."""
    journals, journaled = {}, set()
    for name in sorted(n for n in os.listdir(dirs.archive_fd)
                       if n.startswith(JOURNAL_PREFIX) and n.endswith(".json")):
        got = _journal_claims(dirs.archive_fd, name)
        journals[name] = got[1] if got else "unreadable"
        journaled.update(((got[0] if got else {}).get("claims") or {}).values())
    return {"journals": journals, "sweep": _sweepable(dirs.tasks_fd, journaled)}


def recovery_effects(tasks_dir: Path, archive_dir: Path) -> dict:
    """The recovery state a plan approves, read-only: `transition(authorized=)` refuses
    the whole pass unless it still holds exactly this under the transition lock."""
    try:
        tasks_fd = os.open(tasks_dir, _DIR_FLAGS)
    except OSError:
        return {"journals": {}, "sweep": {}}
    try:
        try:
            archive_fd = os.open(archive_dir.name, _DIR_FLAGS, dir_fd=tasks_fd)
        except OSError:
            return {"journals": {}, "sweep": _sweepable(tasks_fd, set())}
        try:
            view = _Dirs.__new__(_Dirs)
            view.tasks_fd, view.archive_fd = tasks_fd, archive_fd
            return _effects(view)
        finally:
            os.close(archive_fd)
    finally:
        os.close(tasks_fd)


def _recover_all(dirs: _Dirs, results_dir: Path, out: dict,
                 only_sweep: "dict[str, str] | None" = None,
                 render_recovered: "Render | None" = None) -> None:
    journaled: set[str] = set()
    txns: set[str] = set()
    for name in sorted(n for n in os.listdir(dirs.archive_fd)
                       if n.startswith(JOURNAL_PREFIX) and n.endswith(".json")):
        j = _Journal.take(dirs, name)
        if j is None:
            try:  # a live pass's claims are its own, not strays
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dirs.archive_fd)
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    held = json.load(fh)
                journaled.update((held.get("claims") or {}).values())
                txns.add(str(held.get("txn")))
            except (OSError, ValueError, AttributeError):
                txns.add("*")  # unreadable live journal: keep every witness this pass
            continue
        journaled.update((j.data.get("claims") or {}).values())
        _recover(dirs, j, results_dir, out, render_recovered)
        if _exists(name, dirs.archive_fd):
            txns.add(str(j.data.get("txn")))
    _sweep_dead_claims(dirs, journaled, out, only_sweep)
    if "*" not in txns:
        _sweep_witnesses(results_dir, txns, dirs.archive_fd)


# ── the transaction ──────────────────────────────────────────────────────────

def transition(tasks_dir: Path, results_dir: Path, archive_dir: Path, *,
               moves: dict[str, str | None], render: Render, prefix: str,
               now: float | None = None, still_safe: "StillSafe | None" = None,
               arbitrate: "Arbitrate | None" = None, authorized: "dict | None" = None,
               render_recovered: "Render | None" = None) -> dict:
    """Perform the whole transition. `render(landed)` returns the notice for
    the files that landed ({name: archive name}), or None when there is
    nothing to say. `still_safe(name, read)` judges the bytes at `tasks/<read>`
    as task `name`, before the claim and again while holding it; a reason
    refuses that file. `arbitrate(name)` is held around that check-claim-check,
    the lock a task's other owners (the worker router) take. `authorized` is the
    `recovery_effects()` a caller approved: anything else refuses the pass whole.
    `render_recovered(landed, contents)` renders an interrupted pass's notice by the
    caller's current rules from `contents` ({name: (bytes, mtime)} read through one
    descriptor each and bound to the planned generation; None: nothing to report);
    without it the journal's is used.
    One pass at a time holds the transition lock. Returns `result` (the notice's path or None), `body`,
    `archived` ({name: landed name}), `quarantined` ({name: claim name}),
    `recovered` (interrupted passes finished first), `swept` (dead passes'
    unjournaled claims given back) and `errors`."""
    out: dict = {"result": None, "body": None, "archived": {}, "quarantined": {}, "recovered": [],
                 "swept": [], "errors": []}
    dirs, problem = _open_dirs(tasks_dir, archive_dir)
    if dirs is None:
        out["errors"].append(f"{problem}; nothing published, nothing moved")
        return out
    try:
        lock = _transition_lock(dirs)
        if lock is None:
            out["errors"].append("another orphan-check transition holds the lock; nothing published, "
                                 "nothing moved")
            return out
        try:
            return _locked(dirs, results_dir, archive_dir, moves, render, prefix, now, still_safe,
                           arbitrate, authorized, out, render_recovered)
        finally:
            os.close(lock)
    finally:
        dirs.close()


_LOCK_NAME, _LOCK_WAIT_S = ".transition.lock", 10.0


def _transition_lock(dirs: _Dirs) -> "int | None":
    """The one-pass-at-a-time lock in the archive dir; None if not taken in `_LOCK_WAIT_S`."""
    fd = os.open(_LOCK_NAME, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o644, dir_fd=dirs.archive_fd)
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _locked(dirs, results_dir, archive_dir, moves, render, prefix, now, still_safe, arbitrate,
            authorized, out, render_recovered=None) -> dict:
    if True:
        if authorized is not None:
            current = _effects(dirs)
            if current != {"journals": dict(authorized.get("journals") or {}),
                           "sweep": dict(authorized.get("sweep") or {})}:
                out["errors"].append("refused: the recovery state changed since the plan was approved "
                                     "(a journal or a dead pass's claim); nothing published, nothing moved")
                return out
        _recover_all(dirs, results_dir, out,
                     None if authorized is None else dict(authorized.get("sweep") or {}),
                     render_recovered)
        problems = _problems(dirs, moves)
        if problems:
            out["errors"] += [f"{p}; nothing published, nothing moved" for p in problems]
            return out
        if not moves:
            out["body"] = render({})
            if out["body"] is not None:
                try:
                    out["result"] = str(publish_exclusive(results_dir, prefix, out["body"], now))
                except (OSError, TransitionRefused) as exc:
                    out["errors"].append(f"result write failed ({exc}); nothing moved")
            return out
        try:
            j = _Journal.create(dirs, now, {
                "stage": "planned", "txn": secrets.token_hex(8), "prefix": prefix, "moves": moves,
                "body": render({}), "rendered_for": {}, "claims": {}, "landed": {},
                "quarantined": {}, "result": None})
        except OSError as exc:
            out["errors"].append(f"could not write the journal in {archive_dir} ({exc}); nothing moved")
            return out
        try:
            _run_journaled(dirs, j, results_dir, prefix, now, moves, render, out, still_safe, arbitrate)
        except BaseException:
            j.release()  # as a dead process would: the lock goes, the journal stays for recovery
            raise
        return out


def _run_journaled(dirs: _Dirs, j: _Journal, results_dir: Path, prefix: str, now: float | None,
                   moves: dict, render: Render, out: dict, still_safe: "StillSafe | None",
                   arbitrate: "Arbitrate | None" = None) -> None:
    unresolved = False
    for name, generation in moves.items():
        problem = dirs.problem()
        if problem:  # before the journal is touched: it lives in the directory this guards
            out["errors"].append(f"refused to move {name!r}: {problem}")
            continue
        claim = task_archive.claim_name(name)
        j.data["claims"][name] = claim
        nxt = {**j.data["landed"], name: name}
        if still_safe:  # until a safe verdict is durable, recovery returns this claim, never places it
            j.data.setdefault("unjudged", {})[name] = claim
        j.set(stage="claiming", next_for=nxt, next_body=render(nxt))  # on disk before the rename
        def refuse(held: str, name=name) -> None:
            j.data.setdefault("refused", {})[name] = held
            j.set(stage="refusing")

        try:
            _claim(dirs, name, generation, claim, still_safe, arbitrate, refuse)
        except TransitionRefused as exc:
            out["errors"].append(str(exc))
            if getattr(exc, "unresolved", False):
                unresolved = True  # the journaled refusal is finished by the next pass
                continue
            j.data.get("refused", {}).pop(name, None)
            j.data.get("unjudged", {}).pop(name, None)
            if getattr(exc, "kept", None):
                out["quarantined"][name] = j.data["quarantined"][name] = claim
                _mark_quarantine(dirs, claim)
                j.set(stage="claiming")
                continue
            del j.data["claims"][name]  # recovery would read it as never renamed anyway
            try:
                j.set(stage="claiming")
            except OSError:
                pass
            continue
        j.data.get("unjudged", {}).pop(name, None)
        j.set(stage="claimed")
        landed, error, kept, unsynced, pending = _place(dirs, claim, name, generation or "")
        unresolved = unresolved or bool(pending)
        _record_mismatch(dirs, j, name, pending)
        if unsynced:
            j.data.setdefault("unsynced", {})[name] = unsynced
        if landed:
            out["archived"][name] = landed
            j.data["landed"][name] = landed
            j.data["rendered_for"] = dict(j.data["landed"])
            j.data["body"] = render(dict(j.data["landed"]))
        else:
            out["errors"].append(error)
            if kept:
                out["quarantined"][name] = claim
                j.data["quarantined"][name] = claim
                _mark_quarantine(dirs, claim)
        j.set(stage="placed")
    if unresolved:  # a file is neither placed nor returned: the next pass finishes, then reports
        out["errors"].append(f"the notice waits in tasks/archive/{j.name} for the next pass")
        j.release()
        return
    body = render(out["archived"])
    if body is None:
        _finish(j, out)
        return
    out["body"] = _stamp(body, j.data["txn"])
    for _ in range(_NAME_ATTEMPTS):
        name = _mint_name(prefix, now)
        j.set(stage="publishing", result=name, body=out["body"])
        try:
            out["result"] = str(_publish_at(results_dir, name, out["body"], j.data["txn"]))
        except FileExistsError:
            continue  # the one publisher refused the taken name; reserve another
        except (OSError, TransitionRefused) as exc:
            out["errors"].append(f"result write failed ({exc}); the archived files are recorded in "
                                 f"tasks/archive/{j.name}, published by the next pass")
            j.release()
            return
        if _finish(j, out):
            _drop_witness(results_dir, name, j.data["txn"])
        return
    out["errors"].append(f"could not reserve a result name under {results_dir}; the archived files "
                         f"are recorded in tasks/archive/{j.name}, published by the next pass")
    try:  # the last reserved name is another notice's: recovery must not read it as committed
        j.set(stage="placed", result=None)
    except OSError:
        pass
    j.release()


def _finish(j: _Journal, out: dict) -> bool:
    """Remove the journal durably; on a failed barrier the witness must stay."""
    try:
        j.remove()
        return True
    except OSError as exc:
        out["errors"].append(f"the journal tasks/archive/{j.name} could not be removed durably ({exc}); "
                             "the next pass finishes it")
        return False
