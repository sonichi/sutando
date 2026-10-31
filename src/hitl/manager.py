"""HumanRequirement Manager: durable requirement store + projection ledger.

Requirement state here is authority; Matrix events are projections. Each
requirement persists as one JSON file under <workspace>/state/hitl/, written
atomically (temp + os.replace). The projection ledger (last projected revision
+ Matrix event id) makes outbox emission idempotent: re-projecting an already
projected revision is a no-op, an EDIT retry targets the recorded event id,
and neither ever mutates requirement state.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
import dataclasses
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    from ..file_lock import lock_fd, unlock_fd   # the vendored package layout
except ImportError:
    from file_lock import lock_fd, unlock_fd     # the repo's src/ on sys.path

from . import renotify
from .schema import (
    Action,
    ActionReply,
    HumanRequirement,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_IN_PROGRESS,
    STATUS_RESOLVED,
    TERMINAL_STATUSES,
    validate_action,
)


POLICY_DECIDER = "policy"
AUTH_KIND = "auth"
# The requirement id contract, in one place: `save()` refuses anything else and
# `all()` enumerates exactly this shape, so a saved record is never invisible.
ID_RE = re.compile(r"^hitl_[A-Za-z0-9_-]+$")


def _activity(task_ids, to_phase: str, reason: str) -> None:
    """A requirement is the scheduler's WAITING; its end is RUNNING again. Never breaks HITL."""
    if not task_ids:
        return
    try:
        import activity_bus as _bus
        store = _bus.ActivityStore()
        for tid in task_ids:
            store.apply(_bus.LifecycleTransition(tid, to_phase, reason=reason))
    except Exception:  # noqa: BLE001
        pass


# Per process, keyed by store root: ids warned about while still failing, and
# the (count, error, time) last published for health-check.
_ARCHIVE_WARNED: Dict[str, set] = {}
_ARCHIVE_PUBLISHED: Dict[str, tuple] = {}
# The status is rewritten at least this often, so a reader can tell a stopped
# writer (older than ARCHIVE_STATUS_STALE_S) from a steady state.
ARCHIVE_STATUS_REFRESH_S = 300
ARCHIVE_STATUS_STALE_S = 600
# Ledger fields of a refused projection; a projection that lands clears them.
REFUSAL_KEYS = ("refused_revision", "refusals", "retry_at", "refused_error", "failed_revision")


def archive_status_path(store_root: Path) -> Path:
    """Beside the store, like the archive dir, so the store holds only records."""
    root = Path(store_root)
    return root.with_name(root.name + "-archive-status.json")


def archive_health(store_root: Path, now: Optional[float] = None) -> Optional[Dict]:
    """What health-check reads: live record count, the published archive failures and the
    live cards the relay refused past the retry bound, or None when this host has no store. `stale` means a report exists
    but the writer stopped refreshing it, so its count is unknown."""
    root = Path(store_root)
    if not root.is_dir():
        return None
    status: Dict = {}
    try:
        status = json.loads(archive_status_path(root).read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(status, dict):
        status = {}
    failures = int(status.get("failures") or 0)
    updated = status.get("updated_at")
    now = time.time() if now is None else now
    stale = bool(status) and (not isinstance(updated, (int, float))
                              or now - updated > ARCHIVE_STATUS_STALE_S)
    live = sorted(root.glob("hitl_*.json"))
    return {"live": len(live),
            "failures": failures,
            "error": str(status.get("error") or ""),
            "updated_at": updated,
            "reported": bool(status),
            "stale": stale,
            "projection_failed": [p.stem for p in live if _projection_failed_on_disk(p)]}


def _failed(revision: int, ledger: Dict) -> bool:
    return ledger.get("failed_revision") == revision and ledger.get("revision", 0) < revision


def _projection_failed_on_disk(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text())
        return _failed(raw["requirement"]["revision"], raw.get("projection") or {})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def store_roots(workspace: Optional[Path] = None) -> List[Path]:
    """Every requirement store beside the default one (an adapter may keep its own root there)."""
    default = default_store(workspace)
    siblings = sorted(p for p in default.parent.glob(default.name + "-*")
                      if p.is_dir() and not p.name.endswith("-archive"))
    return [default, *siblings]


def default_store(workspace: Optional[Path] = None) -> Path:
    """Where every hitl component on a host keeps requirements: the hook driver
    writes here, the supervisor projects from here. One store, one card. With no
    workspace the repo's resolver is imported here, so a vendored copy never needs it."""
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    return Path(workspace) / "state" / "hitl" / "requirements"


class HitlStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Settled records move here so `all()` stays the size of the live set.
        self.archive_root = self.root.with_name(self.root.name + "-archive")
        self._lock_fd: Optional[int] = None
        self._lock_depth = 0
        self.last_skipped: tuple = ()

    @staticmethod
    def valid_id(req_id: str) -> bool:
        """The one id contract: what `all()` enumerates is exactly what `save()` accepts."""
        return bool(ID_RE.match(req_id or ""))

    def _path(self, req_id: str) -> Path:
        if not self.valid_id(req_id):
            raise ValueError(f"not a requirement id: {req_id!r}")
        return self.root / f"{req_id}.json"

    @contextmanager
    def locked(self):
        """Serialize read-modify-write across processes sharing this store
        (a blocking hook and the bridge do). Re-entrant within one thread."""
        if self._lock_depth == 0:
            self._lock_fd = os.open(str(self.root / ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
            lock_fd(self._lock_fd)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0 and self._lock_fd is not None:
                unlock_fd(self._lock_fd)
                os.close(self._lock_fd)
                self._lock_fd = None

    def save(self, req: HumanRequirement, projection: Optional[Dict] = None) -> None:
        payload = {
            "requirement": _req_to_dict(req),
            "projection": projection
            or self._load_raw(req.id).get("projection", {"revision": 0, "event_id": None}),
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path(req.id))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        # The replace is visible before it is durable; the directory fsync makes it so, or says it could not.
        dfd = os.open(str(self.root), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _load_raw(self, req_id: str) -> Dict:
        if not self.valid_id(req_id):
            return {}  # a wire-supplied id that is not ours: no record, never an error
        p = self._path(req_id)
        # Live first, then the archive: a record may be moved between the two reads.
        for candidate in (p, self.archive_root / p.name):
            try:
                return json.loads(candidate.read_text())
            except (FileNotFoundError, NotADirectoryError):
                continue  # a broken archive dir reads as "no archived copy"
        return {}

    def archive(self, req_id: str) -> bool:
        """Move a record out of the live set, atomically, under the store lock."""
        src = self._path(req_id)
        with self.locked():
            if not src.exists():
                return False
            self.archive_root.mkdir(parents=True, exist_ok=True)
            os.replace(src, self.archive_root / src.name)
            return True

    def report_archive_failures(self, failures: Dict[str, BaseException], now: float) -> None:
        """Take one pulse's archive failures: warn once per failing spell of an id,
        and publish the status for health-check when it changed or needs a refresh."""
        key = str(self.root)
        warned = _ARCHIVE_WARNED.setdefault(key, set())
        for req_id, err in failures.items():
            if req_id not in warned:
                logging.getLogger("hitl.store").warning("hitl: could not archive %s: %s", req_id, err)
        # An id that succeeded or left the live set warns again if it fails later.
        _ARCHIVE_WARNED[key] = set(failures)
        first = next(iter(failures.values()), None)
        error = f"{type(first).__name__}: {first}" if first is not None else ""
        last = _ARCHIVE_PUBLISHED.get(key)
        if last and last[:2] == (len(failures), error) and now - last[2] < ARCHIVE_STATUS_REFRESH_S:
            return
        target = archive_status_path(self.root)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-archive-status-")
            with os.fdopen(fd, "w") as f:
                json.dump({"failures": len(failures), "error": error, "updated_at": now}, f)
            os.replace(tmp, target)
        except OSError:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            return  # reporting never blocks delivery; retried next pulse
        _ARCHIVE_PUBLISHED[key] = (len(failures), error, now)

    def load(self, req_id: str) -> Optional[HumanRequirement]:
        raw = self._load_raw(req_id)
        if not raw:
            return None
        return _req_from_dict(raw["requirement"])

    def projection(self, req_id: str) -> Dict:
        raw = self._load_raw(req_id)
        return raw.get("projection") or {"revision": 0, "event_id": None}

    def all(self) -> List[HumanRequirement]:
        out, skipped = [], []
        for p in sorted(self.root.glob("hitl_*.json")):
            try:
                out.append(_req_from_dict(json.loads(p.read_text())["requirement"]))
            except FileNotFoundError:
                continue  # archived mid-scan: moved, not unreadable
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                skipped.append(p.name)
        # An empty store reads as "nothing needs the human", so a dropped
        # record must leave a trace that a quiet day would not.
        self.last_skipped = tuple(skipped)
        if skipped:
            logging.getLogger("hitl.store").warning(
                "hitl: %d unreadable record(s) skipped in %s: %s",
                len(skipped), self.root, ", ".join(skipped))
        return out


class HitlManager:
    def __init__(self, store: HitlStore, policy=None):
        self.store = store
        # Optional: `decide(req) -> action id | None`; see hitl.policy.
        self.policy = policy

    def create(self, req: HumanRequirement) -> HumanRequirement:
        """Identity is (runtime, kind, device) PLUS the guard, except for auth.

        A differing guard is a different interaction — a second tool call, a
        repainted dialog — and mints a distinct record, so no click can release
        an interaction the human never saw. Auth is the one kind whose payload
        is invariant, so repeat detections collapse onto one refreshed card.
        """
        with self.store.locked():
            for existing in self.active():
                if existing.decided_by == POLICY_DECIDER:
                    continue  # auto-answered, in flight: never a dedup target
                if not (existing.runtime == req.runtime and existing.kind == req.kind
                        and _device_id(existing) == _device_id(req)):
                    continue
                if existing.guard == req.guard:
                    return existing
                if req.kind == AUTH_KIND and req.guard:
                    existing.refresh_guard(req.guard)
                    self.store.save(existing)
                    return existing
            choice = self.policy.decide(req) if self.policy is not None else None
            if choice is not None and any(a.id == choice for a in req.actions):
                req.chosen_action = choice
                req.decided_by = POLICY_DECIDER
                req.transition(STATUS_IN_PROGRESS)
            self.store.save(req)
        if req.decided_by != POLICY_DECIDER and req.status not in TERMINAL_STATUSES:
            _activity(req.blocked_task_ids, "WAITING", req.kind)  # policy-answered: nobody is waiting
        return req

    def active(self) -> List[HumanRequirement]:
        return [r for r in self.store.all() if r.status not in TERMINAL_STATUSES]

    def get(self, req_id: str) -> Optional[HumanRequirement]:
        return self.store.load(req_id)

    def apply_action(self, reply: ActionReply, patch=None) -> Action:
        """Validate the three-layer stale gate; on pass, mark in_progress.

        Returns the matched Action for the caller (driver) to execute.
        Raises StaleRequirementError / MalformedActionError otherwise —
        a stale click must never reach the runtime. `patch(req)`, when given,
        runs after the reload and before the one save, so what it sets on the
        requirement lands in the same write as the acceptance; it is pure and
        merge-preserving and touches nothing else.
        """
        with self.store.locked():
            req = self.store.load(reply.hitl_id)
            if req is None:
                from .schema import MalformedActionError

                raise MalformedActionError(f"no requirement {reply.hitl_id}")
            action = validate_action(req, reply)
            req.chosen_action = action.id
            if reply.answer is not None:
                req.answer = reply.answer
            req.transition(STATUS_IN_PROGRESS)
            if patch is not None:
                patch(req)
            self.store.save(req)
            return action

    def resolve(self, req_id: str) -> List[str]:
        """Mark resolved; returns the blocked task ids to resume."""
        return self._terminate(req_id, STATUS_RESOLVED)

    def cancel(self, req_id: str) -> List[str]:
        return self._terminate(req_id, STATUS_CANCELLED)

    def expire(self, req_id: str) -> List[str]:
        return self._terminate(req_id, STATUS_EXPIRED)

    def _terminate(self, req_id: str, status: str) -> List[str]:
        with self.store.locked():
            req = self.store.load(req_id)
            if req is None:
                return []
            if req.status in TERMINAL_STATUSES:
                return []
            req.transition(status)
            self.store.save(req)
        _activity(req.blocked_task_ids, "RUNNING", status)
        return list(req.blocked_task_ids)

    def link_blocked_task(self, req_id: str, task_id: str) -> None:
        with self.store.locked():
            req = self.store.load(req_id)
            # A terminal record resumes nothing, and saving it would revive an archived one.
            if req is None or req.terminal or task_id in req.blocked_task_ids:
                return
            req.blocked_task_ids.append(task_id)
            self.store.save(req)

    # -- projection ledger ---------------------------------------------------

    def needs_projection(self, req_id: str) -> bool:
        req = self.store.load(req_id)
        if req is None or req.decided_by == POLICY_DECIDER:
            return False  # a policy answer is a record, never a card
        ledger = self.store.projection(req_id)
        if ledger.get("failed_revision") == req.revision:
            return False  # given up on this revision; a later one is tried afresh
        return ledger.get("revision", 0) < req.revision

    def projection_failed(self, req_id: str) -> bool:
        """The relay refused this revision's card past the retry bound, and no send was accepted."""
        req = self.store.load(req_id)
        return req is not None and _failed(req.revision, self.store.projection(req_id))

    def refuse_projection(self, req_id: str, revision: int, error: str, max_refusals: int,
                          retry_fields: Callable[[int], Dict]) -> Optional[Tuple[int, bool]]:
        """Count one refused send of `revision` and give up at `max_refusals`, in one locked
        read-modify-write. -> (attempt, failed), or None when the revision moved on or was accepted."""
        with self.store.locked():
            req = self.store.load(req_id)
            if req is None or req.revision != revision:
                return None
            ledger = self.store.projection(req_id)
            if ledger.get("revision", 0) >= revision or ledger.get("failed_revision") == revision:
                return None
            n = (ledger.get("refusals") or 0) if ledger.get("refused_revision") == revision else 0
            n += 1
            failed = n >= max_refusals
            fields: Dict = {"refused_revision": revision, "refusals": n, "refused_error": error}
            fields.update({"retry_at": None, "failed_revision": revision} if failed else retry_fields(n))
            self.store.save(req, projection={**ledger, **fields})
            return n, failed

    def record_projection(self, req_id: str, revision: int, event_id: Optional[str],
                          extra: Optional[Dict] = None) -> None:
        """Idempotent: recording an older revision than already projected is a
        no-op; the event id (first CREATE event, the EDIT target) is kept."""
        with self.store.locked():
            req = self.store.load(req_id)
            if req is None:
                return
            current = self.store.projection(req_id)
            if revision <= current.get("revision", 0):
                return
            event_id = event_id or current.get("event_id")
            kept = {k: v for k, v in current.items() if k not in REFUSAL_KEYS}
            self.store.save(req, projection={**kept, **(extra or {}),
                                             "revision": revision, "event_id": event_id})

    def record_notice(self, req_id: str, fields: Dict) -> None:
        """Merge follow-up-notice bookkeeping (hitl.renotify) into the ledger."""
        with self.store.locked():
            req = self.store.load(req_id)
            if req is None:
                return
            self.store.save(req, projection={**self.store.projection(req_id), **fields})

    def archive_if_settled(self, req_id: str, now: float) -> bool:
        """Archive a terminal record whose card is current and owes no follow-up notice."""
        with self.store.locked():
            req = self.store.load(req_id)
            if req is None or req.status not in TERMINAL_STATUSES:
                return False
            if self.needs_projection(req_id) or self.projection_failed(req_id):
                return False  # a failed card stays live, where health-check sees it
            if renotify.due(req, self.store.projection(req_id), now) is not None:
                return False
            return self.store.archive(req_id)

    def projection_target(self, req_id: str) -> Optional[str]:
        """Event id of the CREATE projection — the target for status EDITs."""
        return self.store.projection(req_id).get("event_id")


def _device_id(req: HumanRequirement) -> str:
    return str((req.device or {}).get("id") or "")


def _req_to_dict(req: HumanRequirement) -> Dict:
    d = asdict(req)
    d["actions"] = [asdict(a) for a in req.actions]
    return d


_REQ_FIELDS = {f.name for f in dataclasses.fields(HumanRequirement)}


def _req_from_dict(d: Dict) -> HumanRequirement:
    """Unknown keys are dropped: a store shared by two engine revisions must stay
    readable by the older one, and one foreign record must not blind the rest."""
    d = {k: v for k, v in dict(d).items() if k in _REQ_FIELDS}
    d["actions"] = [Action(**a) for a in d.get("actions", [])]
    return HumanRequirement(**d)
