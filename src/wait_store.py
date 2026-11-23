"""Wait markers: one pending owner request that resumes once something outside the core finishes.

A wait is `<workspace>/state/<dirname>/<wait-id>.json`. A claimer first stages the claimed
record, with who takes it and when, as `<wait-id>.<token>.claiming`, then renames the pending
file to `<wait-id>.<token>.taken`: that rename is the claim, so of a waiter, a deadline and a
command exactly one wins and only the winner acts. The winner renames its staged record to
`<wait-id>.claimed`, which therefore names its outcome from the instant it exists. A claimer
stopped in between leaves a `.taken` file whose token names its own staged record, and
`recover` finishes that claim with that outcome. Only a claimer that could not write its
staged record leaves a claim with no outcome; `recover` closes that one as the non-success
outcome its skill names rather than dropping it.

Everything that installs, stamps, un-claims or adopts a claimed record does so while holding
the store's one mutex (`<dirname>.mutex`, beside the directory), and installs only where no claimed record exists, so
a claimed record is never replaced and no one reads it half-made. The mutex is inside `claim`,
`release` and `recover`, so every claimer holds it whether or not it holds the waiter's lock. A waiter holds `<wait-id>.lock`
while it lives, so a dead one is seen and restarted. The winner writes one resume task, which
the core processes like any other task, and then stamps `published_at` on the claimed record.
A claim that owes a task and carries no stamp was interrupted: `recover` publishes it once,
writing the task only when no task of that id is pending, claimed, answered or archived. `owner_task` is the one test of whose request may arm
a wait: a live owner-tier, non-collaborator task of the named source.

Skills supply what differs: the directory name, what a valid marker is and when it is ready.
Standard library only, plus local_task_protocol, task_body_guard and task_envelope; recovery
also asks task_disposition, imported when it runs.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import local_task_protocol as ltp
from task_body_guard import header_safe_value

WAIT_ID_RE = re.compile(r"^[0-9]{13}-[0-9a-f]{8}\Z")
MXID_RE = re.compile(r"^@[^\s:]+:[^\s]+\Z")
CLAIM_KEYS = ("claimed_by", "claimed_at")
PUBLISHED_KEYS = ("published_at", "published")


class NotOwnerTask(ValueError):
    """The task may not arm a wait; str() is the reason, after the task's id."""


def now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_wait_id(t: float) -> str:
    return f"{int(t * 1000):013d}-{secrets.token_hex(4)}"


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def header_values(text: str, key: str) -> list[str]:
    """Every `key:` line anywhere in a task file, the body included."""
    prefix = f"{key}:"
    return [ln[len(prefix):].strip() for ln in text.splitlines() if ln.startswith(prefix)]


class WaitStore:
    """The wait files of one skill under one workspace. `valid(data, wait_id)` says whether a
    marker is one of this skill's; the path and JSON methods are the seams a caller may override."""

    def __init__(self, ws: Path, dirname: str, valid: Callable[[Any, str], bool]) -> None:
        self.ws, self.dirname, self.valid = Path(ws), dirname, valid

    def dir(self) -> Path:
        return self.ws / "state" / self.dirname

    def marker_path(self, wait_id: str) -> Path:
        return self.dir() / f"{wait_id}.json"

    def claimed_path(self, wait_id: str) -> Path:
        return self.dir() / f"{wait_id}.claimed"

    def lock_path(self, wait_id: str) -> Path:
        return self.dir() / f"{wait_id}.lock"

    def read_json(self, path: Path) -> Any:
        return read_json(path)

    def write_json(self, path: Path, data: dict) -> None:
        write_json(path, data)

    def read_marker(self, wait_id: str) -> dict | None:
        """The unclaimed marker, or None once it is claimed. Raises ValueError when unreadable."""
        try:
            data = json.loads(self.marker_path(wait_id).read_text())
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(str(exc)) from None
        if not self.valid(data, wait_id):
            raise ValueError(f"malformed wait marker {wait_id}")
        return data

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def list_markers(self) -> list[dict]:
        out: list[dict] = []
        d = self.dir()
        if not d.is_dir():
            return out
        for p in sorted(d.glob("*.json")):
            if not WAIT_ID_RE.match(p.stem):
                continue
            try:
                marker = self.read_marker(p.stem)
            except ValueError:
                continue
            if marker is not None:
                out.append(marker)
        return out

    def write_marker(self, marker: dict) -> Path:
        self.dir().mkdir(parents=True, exist_ok=True)
        final = self.marker_path(marker["wait_id"])
        self.write_json(final, marker)
        return final

    @contextlib.contextmanager
    def installing(self):
        """Hold the store's mutex: the one place a claimed record is installed, stamped, un-claimed
        or adopted. Blocks for the few file operations another holder is doing; a holder that stops frees it."""
        self.dir().mkdir(parents=True, exist_ok=True)
        with open(self.dir().with_name(self.dir().name + ".mutex"), "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield

    def claim(self, wait_id: str, by: str = "claim", at: float | None = None) -> dict | None:
        """Atomically take the wait. The claimed record, stamped with who took it and when,
        `{"wait_id", "invalid": True}` for a marker that is not valid, or None when someone else won."""
        marker, at = self.marker_path(wait_id), time.time() if at is None else at
        token = secrets.token_hex(4)
        with self.installing():
            data = self.read_json(marker)
            staged = self.dir() / f"{wait_id}.{token}.claiming" if self.valid(data, wait_id) else None
            if staged is not None:
                try:
                    self.write_json(staged, {**data, "claimed_by": by, "claimed_at": at})
                except OSError:
                    staged = None
            taken = self.dir() / f"{wait_id}.{token}.taken"
            try:
                os.rename(marker, taken)
            except FileNotFoundError:
                return None
            if self.claimed_path(wait_id).exists():
                # A claimed record is never replaced: the one already there is this wait's claim.
                self._unlink(taken)
                if staged is not None:
                    self._unlink(staged)
                return None
            os.rename(staged or taken, self.claimed_path(wait_id))
            if staged is not None:
                self._unlink(taken)
            data = self.read_json(self.claimed_path(wait_id))
            if not self.valid(data, wait_id):
                return {"wait_id": wait_id, "invalid": True}
            record = {**data, "claimed_by": by, "claimed_at": at}
            if staged is None:
                try:
                    self.write_json(self.claimed_path(wait_id), record)
                except OSError:
                    pass  # the rename already decided the claim; `recover` closes an unstamped one
            return record

    def finish_taken(self, wait_id: str) -> bool:
        """Complete a claim whose claimer stopped after taking the wait: its own staged record, or
        the bare taken file when it staged none, becomes the claimed record, unless one is installed.
        Taken files, and the staged records of their tokens, are removed. True when one was installed."""
        d, done = self.dir(), False
        if not d.is_dir():
            return done
        with self.installing():
            for taken in sorted(d.glob(f"{wait_id}.*.taken")):
                staged = taken.with_suffix(".claiming")
                if not self.claimed_path(wait_id).exists():
                    try:
                        os.rename(staged if staged.exists() else taken, self.claimed_path(wait_id))
                    except OSError:
                        continue
                    done = True
                self._unlink(taken)
                self._unlink(staged)
        return done

    def taken_ids(self) -> list[str]:
        """Wait ids with a taken file: a claim not finished, or one finished and not tidied."""
        d = self.dir()
        names = sorted(p.name.split(".")[0] for p in d.glob("*.taken")) if d.is_dir() else []
        return sorted({n for n in names if WAIT_ID_RE.match(n)})

    def adopt_unstamped(self, wait_id: str, by: str, at: float) -> dict | None:
        """Give a claimed record that names no outcome the outcome `by`, on disk; the record, or None
        when it names one already, is not valid, or cannot be written."""
        with self.installing():
            record = self.read_json(self.claimed_path(wait_id))
            if not self.valid(record, wait_id) or record.get("claimed_by") is not None:
                return None
            record = {**record, "claimed_by": by, "claimed_at": at, "unstamped": True}
            try:
                self.write_json(self.claimed_path(wait_id), record)
            except OSError:
                return None
            return record

    def release(self, record: dict) -> bool:
        """Undo a claim whose resume task could not be written, so the next poll or rearm retries.
        False when it cannot be put back, or the claimed record is no longer this claimer's own."""
        wait_id = record["wait_id"]
        with self.installing():
            current = self.read_json(self.claimed_path(wait_id))
            if isinstance(current, dict) and (current.get("published_at") is not None or current.get(
                    "claimed_by") not in (None, record.get("claimed_by"))):
                return False
            try:
                self.write_json(self.claimed_path(wait_id), {k: v for k, v in record.items() if k not in CLAIM_KEYS})
            except OSError:
                pass
            try:
                os.rename(self.claimed_path(wait_id), self.marker_path(wait_id))
            except OSError:
                return False
            return True

    def mark_published(self, record: dict, at: float, how: str = "task") -> dict:
        """Stamp the claimed record: its resume task is out (`task`, or `found` already there),
        or it is too old to send one (`stale`)."""
        record = {**record, "published_at": at, "published": how}
        self.write_json(self.claimed_path(record["wait_id"]), record)
        return record

    def unpublished(self, outcomes: tuple, unstamped: bool = False) -> list[dict]:
        """Claimed records that owe a resume task (claimed_by in `outcomes`) and carry no published
        stamp; with `unstamped`, also the ones that name no outcome at all."""
        out: list[dict] = []
        d = self.dir()
        if not d.is_dir():
            return out
        for p in sorted(d.glob("*.claimed")):
            record = self.read_json(self.claimed_path(p.stem)) if WAIT_ID_RE.match(p.stem) else None
            owes = self.valid(record, p.stem) and (
                record.get("claimed_by") in outcomes or (unstamped and record.get("claimed_by") is None))
            if owes and record.get("published_at") is None:
                out.append(record)
        return out

    def acquire_lock(self, wait_id: str):
        """An exclusive, non-blocking hold on the wait's lock file; None when a live waiter has it."""
        self.dir().mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path(wait_id), "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh

    def waiter_alive(self, wait_id: str) -> bool:
        fh = self.acquire_lock(wait_id)
        if fh is None:
            return True
        fh.close()
        return False

    def prune(self, now: float, older_than_s: float) -> int:
        """Drop claimed records, staged claims, and locks whose marker is gone, last touched `older_than_s`
        ago; and a taken file whose claim is installed, whatever its age."""
        removed = 0
        d = self.dir()
        if not d.is_dir():
            return removed
        for p in d.iterdir():
            if p.suffix not in (".claimed", ".lock", ".claiming", ".taken") \
                    or (p.suffix == ".lock" and self.marker_path(p.stem).exists()):
                continue
            # A taken file is the wait itself until its claim is installed; after that it is litter.
            wait_id = p.name.split(".")[0]
            if p.suffix == ".taken" and not self.claimed_path(wait_id).exists():
                continue
            try:
                if p.suffix == ".taken" or now - p.stat().st_mtime >= older_than_s:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def hold_lock(store: WaitStore, wait_id: str):
    """The waiter's lock with this process's pid written in it, or None when another waiter holds it."""
    lock = store.acquire_lock(wait_id)
    if lock is None:
        return None
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def publish(store: WaitStore, record: dict, write: Callable[[dict], Any], at: float) -> None:
    """Write the claimed wait's resume task, then stamp the claim. A write that fails raises and
    leaves no stamp; a stamp that fails is left to `recover`, which finds the task by its id."""
    write(record)
    try:
        store.mark_published(record, at)
    except OSError:
        pass


def task_known(ws: Path, task_id: str) -> bool:
    """Whether a task of this id is pending, claimed, answered or archived in the workspace."""
    import task_disposition  # noqa: PLC0415

    return task_disposition.task_disposition(ws, task_id) != task_disposition.MISSING


def recover_one(store: WaitStore, ws: Path, wait_id: str, *, outcomes: tuple, task_id: Callable[[dict], str],
                write: Callable[[dict], Any], now: float, stale_after_s: float,
                unstamped_as: str | None = None) -> str | None:
    """Publish one interrupted claim; the caller holds the wait's lock. Returns task | found |
    stale, or None when the claim owes nothing, is stamped already, or the task could not be written.
    A claim that names no outcome is closed as `unstamped_as`."""
    store.finish_taken(wait_id)
    if unstamped_as is not None:
        store.adopt_unstamped(wait_id, unstamped_as, now)
    record = store.read_json(store.claimed_path(wait_id))
    if not store.valid(record, wait_id) or record.get("published_at") is not None:
        return None
    if record.get("claimed_by") not in outcomes:
        return None
    if now - float(record.get("claimed_at") or 0) >= stale_after_s:
        how = "stale"
    elif task_known(ws, task_id(record)):
        how = "found"
    else:
        try:
            write(record)
        except (OSError, ValueError):
            return None
        how = "task"
    try:
        store.mark_published(record, now, how)
    except OSError:
        pass
    return how


def recover(store: WaitStore, ws: Path, *, outcomes: tuple, task_id: Callable[[dict], str],
            write: Callable[[dict], Any], now: float, stale_after_s: float,
            unstamped_as: str | None = None) -> list[dict]:
    """Publish every interrupted claim whose waiter is gone; a wait whose lock is held is left to its holder."""
    out = []
    owing = [r["wait_id"] for r in store.unpublished(outcomes, unstamped=unstamped_as is not None)]
    for wait_id in sorted(set(owing + store.taken_ids())):
        lock = hold_lock(store, wait_id)
        if lock is None:
            continue
        try:
            how = recover_one(store, ws, wait_id, outcomes=outcomes, task_id=task_id, write=write,
                              now=now, stale_after_s=stale_after_s, unstamped_as=unstamped_as)
        finally:
            lock.close()
        if how:
            out.append({"wait_id": wait_id, "published": how})
    return out


def spawn_detached(script: Path, ws: Path, wait_id: str, log_name: str) -> int:
    """Start `script --workspace WS waiter WAIT_ID` in its own session, so it outlives the core."""
    logs = Path(ws) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / log_name, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(script), "--workspace", str(ws), "waiter", wait_id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ws),
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


def _stamp(text: str, ws: Path) -> str:
    try:
        import task_envelope  # noqa: PLC0415

        return task_envelope.stamp_text(text, ws)
    except Exception:  # noqa: BLE001 — a stamping error costs the stamp, never the task
        return text


def write_resume_task(ws: Path, task_id: str, *, source: str, room: str, owner: str, body: str,
                      now: float) -> Path:
    """One owner-tier task for the core, addressed to the wait's room. `body` is already confined."""
    headers = [
        ("id", task_id),
        ("timestamp", now_iso(now)),
        ("source", source),
        ("interaction_type", "message"),
        ("channel_id", header_safe_value(room)),
        ("user_id", header_safe_value(owner)),
        ("access_tier", "owner"),
        ("priority", "normal"),
    ]
    ltp.set_task_stamper(lambda text: _stamp(text, ws))
    try:
        return ltp.write_task_file(Path(ws) / "tasks", task_id, headers, body)
    finally:
        ltp.set_task_stamper(None)


def owner_task(ws: Path, task: str, source: str, source_name: str = "") -> tuple[str, str]:
    """(owner mxid, task text) of `task` when it is a live owner-tier, non-collaborator task from
    `source` (`source_name` in the refusal). Anything else raises NotOwnerTask: only the owner's own request may arm an owner resume."""
    try:
        text = (Path(ws) / "tasks" / f"{task}.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise NotOwnerTask("is not a live task in this workspace") from None
    above_body = ltp.parse_task_headers(text).headers
    if above_body.get("id") != task:
        raise NotOwnerTask("does not carry its own id")
    # The gateway writes source above the body and one-lines every value; any other
    # header line anywhere in the file counts against the task, never for it.
    if above_body.get("source") != source or set(header_values(text, "source")) != {source}:
        raise NotOwnerTask(f"is not from {source_name or source}")
    if header_values(text, "collaborator"):
        raise NotOwnerTask("is a collaborator task")
    tiers = {ltp.canonical_access_tier(v) for v in header_values(text, "access_tier")}
    if tiers != {"owner"}:
        raise NotOwnerTask("is not owner tier")
    users = set(header_values(text, "user_id"))
    if len(users) != 1 or not MXID_RE.match(next(iter(users))):
        raise NotOwnerTask("has no single user_id")
    try:
        import task_envelope  # noqa: PLC0415

        verdict = task_envelope.verify_text(text, ws).get("verdict")
    except Exception:  # noqa: BLE001 — an unverifiable stamp is soak-mode telemetry, not a refusal
        verdict = None
    if verdict == "invalid":
        raise NotOwnerTask("has an envelope stamp that does not verify")
    return next(iter(users)), text
