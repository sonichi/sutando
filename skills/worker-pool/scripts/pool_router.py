#!/usr/bin/env python3
"""The router pass: resolve one admitted task against the roster, write deliveries.

A role with a contract, not a process. This is a function the core's task watcher
calls — there is no daemon here, and there must not be one: a second process would
re-watch `tasks/`, which the watcher already does across code hardened by many
incidents.

Its whole input is the roster, one task, and the deliveries already made for
that task (attribution, or a sentinel in any recipient folder), which is what
makes a pass testable by replay: same roster, same task, same deliveries, with
no clock and no liveness probe in the decision.

The one thing it may NOT do is substitute one WORKER for another. A target not on
the roster goes to the core, which is a recipient rather than a fallback.
recipient is how a task reaches someone the owner never addressed.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# helpers live in the core; repo root is parents[3] from this directory
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pool_attribution as pa  # noqa: E402
import pool_delivery as pd  # noqa: E402
import pool_record as prec  # noqa: E402
import pool_roster as pr  # noqa: E402

PRIORITY_ORDER = {"urgent": 0, "normal": 1, "low": 2}


class RouterRefused(Exception):
    """The pass cannot run. Nothing was delivered."""


def order_candidates(tasks) -> list:
    """`urgent > normal > low`, then oldest payload first. A task with no
    priority sorts as `normal` rather than last — an absent field is a default,
    not a demotion."""
    return sorted(
        tasks,
        key=lambda t: (PRIORITY_ORDER.get((t.get("priority") or "normal"), 1),
                       t.get("created_at") or "", t.get("id") or ""))


class ConflictingDelivery(RouterRefused):
    """Two recipients hold the task, or the record names one and the fact another."""


@contextlib.contextmanager
def task_arbitration(workspace, task_id: str):
    """One lock per TASK around the whole decision: find what is committed,
    then attribute or create. Two passes racing the same task with different
    bindings would otherwise each take a different folder lock and make twins."""
    d = pa.attribution_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f".{task_id}.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class UnreadableEvidence(RouterRefused):
    """A sentinel, folder or record could not be READ: absent and unreadable are not the same."""


def holders(workspace, task_id: str, _between_suffix_checks=None, _arbitration_seams=None) -> list[str]:
    """Every recipient folder holding a sentinel for `task_id`, under any suffix.

    Tri-state per name: regular is held, absent is not, anything else refuses
    (`find()`'s None conflates absent with unreadable). Each folder's names are
    read under that folder's arbitration lock, the one accept/release take, so
    a release renaming `.accepted` to `.txt` cannot slip between two checks;
    the task lock is already held by the caller, so the order is task, folder.
    Which entries are recipients is the shared record policy's call
    (`pool_record.iter_recipients`): an alias raises there, before any read
    or lock here, so an alias never gains a lock.
    """
    root = pd.deliveries_dir(workspace, pr.CORE).parent
    try:
        # The shared recipient policy: non-recipient names and non-directories are
        # skipped, a recipient-named symlink raises, an unlistable root raises.
        names = prec.iter_recipients(root) if root.is_dir() else []
    except OSError as e:
        raise UnreadableEvidence(f"{task_id}: cannot read the recipient folders under {root}: {e}") from e
    held = []
    for name_ in names:
        folder = root / name_
        seams = _arbitration_seams or {}
        try:
            lock = pd.arbitration(workspace, folder.name, **seams)
            dfd = lock.__enter__()
        except OSError as e:
            raise UnreadableEvidence(f"{task_id}: cannot lock {folder}: {e}") from e
        try:
            for name in pd.sentinel_names(task_id):
                state = pd.regular_file_state(name, dir_fd=dfd)     # anchored: never the path again
                if _between_suffix_checks:
                    _between_suffix_checks(folder / name, state)
                if state == "regular":
                    held.append(folder.name)
                    break
                if state != "absent":
                    raise UnreadableEvidence(f"{task_id}: {folder / name} is {state}; refusing to decide")
        finally:
            lock.__exit__(None, None, None)
    return held


def attribution_state(workspace, task_id: str) -> tuple[str, str | None]:
    """('absent' | 'worker' | 'malformed' | 'unreadable', worker id)."""
    path = pa.attribution_path(workspace, task_id)
    state = pd.regular_file_state(path)
    if state == "absent":
        return "absent", None
    if state != "regular":
        return "unreadable", None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unreadable", None
    return ("worker", value) if pa.is_worker_id(value) else ("malformed", None)


def committed_recipient(workspace, task_id: str, _between_suffix_checks=None, _arbitration_seams=None) -> str | None:
    """The recipient a delivery for `task_id` already committed to, or None.

    Attribution is the record; a sentinel in ANY recipient folder is the fact
    behind it (a crash can leave the fact without the record). A replay must
    follow this, never the current binding. Two holders, a record naming one
    recipient while the fact names another, a record that is present but
    malformed, or evidence that cannot be read: all refused, so the anomaly
    surfaces instead of being resolved by whichever pass runs next.
    """
    state, worker = attribution_state(workspace, task_id)
    if state == "unreadable":
        raise UnreadableEvidence(f"{task_id}: attribution record unreadable; refusing to decide")
    if state == "malformed":
        raise ConflictingDelivery(f"{task_id}: attribution record present but malformed; refusing to decide")
    held = holders(workspace, task_id, _between_suffix_checks, _arbitration_seams)
    if len(held) > 1 or (worker is not None and held and held != [worker]):
        raise ConflictingDelivery(
            f"{task_id}: attribution={worker!r} sentinels in {held}; refusing to choose")
    if worker is not None:
        return worker
    return held[0] if held else None


def deliver_one(workspace, recipient: str, task_id: str,
                _between_sentinel_and_attribution=None, _arbitration_seams=None) -> str:
    """Create one sentinel. Returns 'delivered' | 'already' | 'no-payload'.

    Checks BOTH names: a sentinel already renamed to `.accepted` is work in
    flight, and recreating the pending name would deliver it a second time.
    `EEXIST` is success — another pass won the race and the delivery exists.

    A sentinel names a payload; without one it names nothing, and its recipient
    can only delete it. An ARCHIVED payload is the dangerous case — that is
    finished work, and a sentinel would offer it again.

    `_between_sentinel_and_attribution` is a test seam: the two writes are not
    crash-atomic, so an existing sentinel is re-attributed on 'already'.
    """
    # Check-of-both-names and create are ONE transition under the folder's lock,
    # through its directory fd: an accept between them cannot slip a rename in.
    with pd.arbitration(workspace, recipient, **(_arbitration_seams or {})) as dfd:
        if pd.find_in(dfd, task_id) is not None:
            _attribute(workspace, recipient, task_id)
            return "already"
        if not pd.payload_path(Path(workspace), task_id).is_file():
            return "no-payload"
        try:
            os.close(os.open(task_id + pd.PENDING_SUFFIX, os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dfd))
        except FileExistsError:
            _attribute(workspace, recipient, task_id)
            return "already"
        if _between_sentinel_and_attribution:
            _between_sentinel_and_attribution()
        # Under the SAME lock as the sentinel: attribution and delivery are one
        # fact. Recording earlier attributed refusals that never delivered.
        _attribute(workspace, recipient, task_id)
    return "delivered"


def _attribute(workspace, recipient: str, task_id: str) -> None:
    """Idempotent: `record` is first-write-wins, and the core is never attributed."""
    if recipient != pr.CORE:
        pa.record(workspace, task_id, recipient)


def route(workspace, task: dict, roster=None, _between_suffix_checks=None, _arbitration_seams=None) -> dict:
    """One task through one pass.

    `roster=None` loads it; an absent or unreadable roster REFUSES the pass
    rather than defaulting to the core, which would aim every task at one
    recipient the moment the file is unwritable.
    """
    task_id = task.get("id")
    if not task_id:
        raise RouterRefused("task has no id")

    # One per-task lock around the whole decision; the commit is resolved before
    # the roster is needed, so a replay finishes it even if the roster is gone.
    with task_arbitration(workspace, task_id):
        committed = committed_recipient(workspace, task_id, _between_suffix_checks, _arbitration_seams)
        if committed is not None:
            targets, unknown, r = [committed], [], (roster if roster is not None else {})
        else:
            r = roster if roster is not None else pr.load_roster(workspace)
            if r is None:
                raise RouterRefused("roster is absent or unreadable — refusing the pass")
            source = task.get("channel_id") or task.get("source") or ""
            try:
                targets = pr.targets_for(r, source, pr.requested_worker_of(task))
            except pr.AmbiguousWorkerName as e:
                raise RouterRefused(str(e)) from e

            # A name not on the roster is not a worker: the core takes it. The core is
            # a recipient, not a fallback, and no OTHER worker ever gets the task.
            unknown = pr.unknown_targets(r, targets)
            if unknown:
                targets = [pr.CORE]

        # Liveness is not consulted: a durable sentinel is found by a worker that starts later.
        # "no-payload" stays its own list: folded into `already` a refusal reads as delivered.
        buckets = {"delivered": [], "already": [], "no-payload": []}
        for t in targets:
            buckets[deliver_one(workspace, t, task_id, _arbitration_seams=_arbitration_seams)].append(t)
    return {"task_id": task_id, "version": r.get("version"), "targets": targets,
            "delivered": buckets["delivered"], "already": buckets["already"],
            "skipped": buckets["no-payload"],
            "redirected": unknown, "error": None}


def route_all(workspace, tasks, roster=None) -> list:
    """A pass over many tasks, in priority order. The roster is read ONCE so
    every task in one pass is decided against the same version."""
    r = roster if roster is not None else pr.load_roster(workspace)
    if r is None:
        raise RouterRefused("roster is absent or unreadable — refusing the pass")
    return [route(workspace, t, r) for t in order_candidates(tasks)]
