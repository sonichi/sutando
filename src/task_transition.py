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

  Journal. A journal in the archive dir (`.notice-<second>-<pid>-<token>.json`,
  beside a flock'd `.lock` the owner holds while alive) records the stage:
  `planned` (moves and the planned notice), then per file its private claim
  name BEFORE the rename to it (`claiming`), then where it landed or that it
  was quarantined, then `publishing` with the reserved notice name. It is
  removed when the notice is committed. A recorded claim is in progress until
  the journal says otherwise: never read as "gone".

  Per file (`task_archive.claim_generation` / `place_claim`): the live name is
  taken by one atomic rename into `<name>.claim-<pid>-<token>` in the same
  directory (same filesystem, never a copy), judged through its own descriptor
  against the planned generation, and only a match is placed by the no-clobber
  move through the handles (`.N` on a taken name). A mismatched claim is linked
  back to its live name without replacing; if a writer already recreated that
  name, the claimed bytes stay quarantined under the claim name, recorded, and
  reported. No live name is ever unlinked by this code or by `task_archive`.

  Notice. Rendered from the FINAL landed names, published last at the reserved
  name through `result_publish`'s exclusive mode. A body over
  PROACTIVE_MAX_BODY_B is refused: the delivery adapter dead-letters it.

  Recovery. The next pass takes each journal whose lock is free (its owner is
  gone) and continues it from the recorded stage: an unplaced claim is placed
  (or found in the archive by its generation when the crash followed the
  placement), a reserved notice is published unless it already exists, and the
  journal is removed. Each generation ends with exactly one copy, live or
  archived; one notice exists iff its publication committed; a second recovery
  pass finds nothing to do.
"""
from __future__ import annotations

import errno
import fcntl
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


def _publish_at(results_dir: Path, name: str, body: str) -> Path:
    """Publish `body` at exactly `results_dir/name`, whole, never replacing.
    Raises TransitionRefused for a body the adapter could never deliver,
    FileExistsError when the name is already committed; OSError propagates."""
    size = len(body.encode("utf-8"))
    if size > PROACTIVE_MAX_BODY_B:
        raise TransitionRefused(f"body is {size} bytes (> {PROACTIVE_MAX_BODY_B}); undeliverable")
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:  # a file holds the directory's name: not a taken result name
        raise NotADirectoryError(errno.ENOTDIR, f"{results_dir} is not a directory") from exc
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
        os.close(self.lock_fd)

    def remove(self) -> None:
        try:
            os.unlink(self.name, dir_fd=self.dirs.archive_fd)
        except FileNotFoundError:
            pass
        self._drop_lock()

    def release(self) -> None:
        """Leave the journal for recovery; only the owner's hold on the lock goes."""
        os.close(self.lock_fd)


def pending_notices(archive_dir: Path) -> list[str]:
    """Journals an interrupted pass left behind, read-only, for the plan."""
    try:
        return sorted(n for n in os.listdir(archive_dir)
                      if n.startswith(JOURNAL_PREFIX) and n.endswith(".json"))
    except OSError:
        return []


# ── the per-file move ────────────────────────────────────────────────────────

def _claim(dirs: _Dirs, name: str, generation: str | None, claim: str | None = None) -> str:
    problem = dirs.problem() or _source_problem(dirs.tasks_fd, name, generation)
    if problem:
        raise TransitionRefused(f"refused to move {name!r}: {problem}")
    _checkpoint("before_claim", name)
    try:
        claim = task_archive.claim_generation(name, src_dir_fd=dirs.tasks_fd, claim=claim)
    except FileNotFoundError:
        raise TransitionRefused(f"refused to move {name!r}: no longer in tasks/ — another pass "
                                "archived it and sent its notice") from None
    _checkpoint("after_claim", name)
    return claim


def _place(dirs: _Dirs, claim: str, name: str, generation: str) -> "tuple[str | None, str | None, bool]":
    """(landed name, error line, quarantined): one of the first two is set; the
    flag says the claim stayed in tasks/ as the quarantine."""
    try:
        landed = task_archive.place_claim(claim, name, generation, name, src_dir_fd=dirs.tasks_fd,
                                          dst_dir_fd=dirs.archive_fd)
        return landed.name, None, False
    except task_archive.GenerationChanged as exc:
        if exc.kept_as:
            return None, (f"refused to move {name!r}: {exc.why}; its name was taken again, so it is "
                          f"quarantined as tasks/{exc.kept_as} — rename it back to requeue"), True
        return None, (f"refused to move {name!r}: {exc.why}; it is back under its name, live, and "
                      "the next pass reports it"), False


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
        landed, error, _ = _place(dirs, claim, name, generation or "")
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


def _sweep_dead_claims(dirs: _Dirs, journaled: set[str], out: dict) -> None:
    """A `<name>.claim-<pid>-…` in tasks/ that no journal names and whose pid is
    gone was left by a pass that died between its claim and its record (or by a
    journal since removed): there is no generation to verify it against, so it
    is given back to its live name without replacing, for the next plan to
    judge; when that name is taken it stays, reported, for the operator."""
    for entry in sorted(os.listdir(dirs.tasks_fd)):
        m = _CLAIM_RE.match(entry)
        if not m or entry in journaled or _pid_alive(int(m.group("pid"))):
            continue
        if _exists(entry + QUARANTINE_SUFFIX, dirs.tasks_fd):
            continue  # kept on purpose, named in its pass's errors
        live = m.group("live")
        try:
            os.link(entry, live, src_dir_fd=dirs.tasks_fd, dst_dir_fd=dirs.tasks_fd)
        except FileExistsError:
            out["errors"].append(f"orphaned claim tasks/{entry} from a dead pass; its name {live!r} is "
                                 "taken, so it is kept — rename it back to requeue")
            continue
        except OSError as exc:
            out["errors"].append(f"orphaned claim tasks/{entry} from a dead pass could not be given back ({exc})")
            continue
        os.unlink(entry, dir_fd=dirs.tasks_fd)
        task_archive._fsync_dir(dirs.tasks_fd)
        out["swept"].append({"claim": entry, "live": live})


def _exists(name: str, dir_fd: int | None = None) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
        return True
    except OSError:
        return False


def _recover(dirs: _Dirs, j: _Journal, results_dir: Path, out: dict) -> None:
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
        _continue(dirs, j, results_dir, report)
    except OSError as exc:
        report["errors"].append(f"recovery of tasks/archive/{j.name} interrupted ({exc}); retried next pass")
        j.release()
    out["errors"] += report["errors"]


def _continue(dirs: _Dirs, j: _Journal, results_dir: Path, report: dict) -> None:
    d = j.data
    claims, landed = dict(d.get("claims") or {}), dict(d.get("landed") or {})
    quarantined = dict(d.get("quarantined") or {})
    for name, generation in d["moves"].items():
        if name in landed or name in quarantined or not claims.get(name):
            continue  # placed, quarantined, or never claimed (still live; planned afresh)
        claim = claims[name]
        if _exists(claim, dirs.tasks_fd):
            got, error, kept = _place(dirs, claim, name, generation)  # recorded, renamed, unplaced
            if got:
                landed[name] = got
            else:
                report["errors"].append(error)
                if kept:
                    quarantined[name] = claim
                    _mark_quarantine(dirs, claim)
        elif _source_problem(dirs.tasks_fd, name, generation) is None:
            del claims[name]  # recorded but never renamed: still live under its name, planned afresh
        else:
            found = _find_placed(dirs, name, generation)
            if found:
                landed[name] = found  # renamed and placed; the crash hid the record
            else:
                report["errors"].append(f"lost track of {name!r}: its claim {claim} is gone and no "
                                        "archived copy carries its generation; check tasks/archive/")
        j.set(claims=claims, landed=landed, quarantined=quarantined)
    report["landed"] = landed
    body, name = d.get("body"), d.get("result")
    if body is not None and (landed or name):
        if d.get("stage") != "publishing":
            renamed = {k: v for k, v in landed.items() if k != v}
            note = "".join(f"\n{k} is archived as tasks/archive/{v}." for k, v in sorted(renamed.items()))
            body = f"{body.rstrip()}\n\n(Published by a later pass after an interrupted one.){note}\n"
            name = _mint_name(d.get("prefix") or "proactive-", None)
            j.set(stage="publishing", result=name, body=body)
        if _exists(str(results_dir / name)):
            report["result"] = str(results_dir / name)  # committed before the crash: no second notice
        else:
            try:
                report["result"] = str(_publish_at(results_dir, name, body))
            except FileExistsError:
                report["result"] = str(results_dir / name)
            except (OSError, TransitionRefused) as exc:
                report["errors"].append(f"could not publish tasks/archive/{j.name} ({exc}); retried next pass")
                j.release()
                return
    j.remove()


def _recover_all(dirs: _Dirs, results_dir: Path, out: dict) -> None:
    journaled: set[str] = set()
    for name in sorted(n for n in os.listdir(dirs.archive_fd)
                       if n.startswith(JOURNAL_PREFIX) and n.endswith(".json")):
        j = _Journal.take(dirs, name)
        if j is None:
            try:  # a live pass's claims are its own, not strays
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=dirs.archive_fd)
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    journaled.update((json.load(fh).get("claims") or {}).values())
            except (OSError, ValueError, AttributeError):
                pass
            continue
        journaled.update((j.data.get("claims") or {}).values())
        _recover(dirs, j, results_dir, out)
    _sweep_dead_claims(dirs, journaled, out)


# ── the transaction ──────────────────────────────────────────────────────────

def transition(tasks_dir: Path, results_dir: Path, archive_dir: Path, *,
               moves: dict[str, str | None], render: Render, prefix: str,
               now: float | None = None) -> dict:
    """Perform the whole transition. `render(landed)` returns the notice for
    the files that landed ({name: archive name}), or None when there is
    nothing to say. Returns `result` (the notice's path or None), `body`,
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
        _recover_all(dirs, results_dir, out)
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
                "stage": "planned", "prefix": prefix, "moves": moves,
                "body": render({n: n for n in moves}), "claims": {}, "landed": {},
                "quarantined": {}, "result": None})
        except OSError as exc:
            out["errors"].append(f"could not write the journal in {archive_dir} ({exc}); nothing moved")
            return out
        try:
            _run_journaled(dirs, j, results_dir, prefix, now, moves, render, out)
        except BaseException:
            j.release()  # as a dead process would: the lock goes, the journal stays for recovery
            raise
        return out
    finally:
        dirs.close()


def _run_journaled(dirs: _Dirs, j: _Journal, results_dir: Path, prefix: str, now: float | None,
                   moves: dict, render: Render, out: dict) -> None:
    for name, generation in moves.items():
        problem = dirs.problem()
        if problem:  # before the journal is touched: it lives in the directory this guards
            out["errors"].append(f"refused to move {name!r}: {problem}")
            continue
        claim = task_archive.claim_name(name)
        j.data["claims"][name] = claim
        j.set(stage="claiming")  # the name is on disk before anything carries it
        try:
            _claim(dirs, name, generation, claim)
        except TransitionRefused as exc:
            out["errors"].append(str(exc))
            del j.data["claims"][name]  # recovery would read it as never renamed anyway
            try:
                j.set(stage="claiming")
            except OSError:
                pass
            continue
        j.set(stage="claimed")
        landed, error, kept = _place(dirs, claim, name, generation or "")
        if landed:
            out["archived"][name] = landed
            j.data["landed"][name] = landed
        else:
            out["errors"].append(error)
            if kept:
                out["quarantined"][name] = claim
                j.data["quarantined"][name] = claim
                _mark_quarantine(dirs, claim)
        j.set(stage="placed")
    out["body"] = render(out["archived"])
    if out["body"] is None:
        j.remove()
        return
    for _ in range(_NAME_ATTEMPTS):
        name = _mint_name(prefix, now)
        j.set(stage="publishing", result=name, body=out["body"])
        try:
            out["result"] = str(_publish_at(results_dir, name, out["body"]))
        except FileExistsError:
            continue  # the one publisher refused the taken name; reserve another
        except (OSError, TransitionRefused) as exc:
            out["errors"].append(f"result write failed ({exc}); the archived files are recorded in "
                                 f"tasks/archive/{j.name}, published by the next pass")
            j.release()
            return
        j.remove()
        return
    out["errors"].append(f"could not reserve a result name under {results_dir}; the archived files "
                         f"are recorded in tasks/archive/{j.name}, published by the next pass")
    j.release()
