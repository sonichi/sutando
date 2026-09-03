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
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
import dataclasses
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from workspace_default import resolve_workspace

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


def default_store(workspace: Optional[Path] = None) -> Path:
    """Where every hitl component on a host keeps requirements: the hook driver
    writes here, the supervisor projects from here. One store, one card."""
    ws = Path(workspace) if workspace is not None else resolve_workspace()
    return ws / "state" / "hitl" / "requirements"


class HitlStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
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
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0 and self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
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

    def _load_raw(self, req_id: str) -> Dict:
        if not self.valid_id(req_id):
            return {}  # a wire-supplied id that is not ours: no record, never an error
        p = self._path(req_id)
        if not p.exists():
            return {}
        return json.loads(p.read_text())

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
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
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
            return req

    def active(self) -> List[HumanRequirement]:
        return [r for r in self.store.all() if r.status not in TERMINAL_STATUSES]

    def get(self, req_id: str) -> Optional[HumanRequirement]:
        return self.store.load(req_id)

    def apply_action(self, reply: ActionReply) -> Action:
        """Validate the two-layer stale gate; on pass, mark in_progress.

        Returns the matched Action for the caller (driver) to execute.
        Raises StaleRequirementError / MalformedActionError otherwise —
        a stale click must never reach the runtime.
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
            return list(req.blocked_task_ids)

    def link_blocked_task(self, req_id: str, task_id: str) -> None:
        with self.store.locked():
            req = self.store.load(req_id)
            if req is None or task_id in req.blocked_task_ids:
                return
            req.blocked_task_ids.append(task_id)
            self.store.save(req)

    # -- projection ledger ---------------------------------------------------

    def needs_projection(self, req_id: str) -> bool:
        req = self.store.load(req_id)
        if req is None or req.decided_by == POLICY_DECIDER:
            return False  # a policy answer is a record, never a card
        return self.store.projection(req_id).get("revision", 0) < req.revision

    def record_projection(self, req_id: str, revision: int, event_id: Optional[str]) -> None:
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
            self.store.save(req, projection={"revision": revision, "event_id": event_id})

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
