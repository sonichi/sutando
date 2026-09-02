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
import os
import tempfile
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


def default_store(workspace: Optional[Path] = None) -> Path:
    """Where every hitl component on a host keeps requirements: the hook driver
    writes here, the supervisor projects from here. One store, one card."""
    ws = Path(workspace) if workspace is not None else resolve_workspace()
    return ws / "state" / "hitl" / "requirements"


class HitlStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, req_id: str) -> Path:
        safe = "".join(c for c in req_id if c.isalnum() or c in "_-")
        return self.root / f"{safe}.json"

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
        out = []
        for p in sorted(self.root.glob("hitl_*.json")):
            try:
                out.append(_req_from_dict(json.loads(p.read_text())["requirement"]))
            except (json.JSONDecodeError, KeyError):
                continue
        return out


class HitlManager:
    def __init__(self, store: HitlStore, policy=None):
        self.store = store
        # Optional: `decide(req) -> action id | None`; see hitl.policy.
        self.policy = policy

    def create(self, req: HumanRequirement) -> HumanRequirement:
        # One active per (runtime, kind, device): re-detection refreshes the
        # guard; two sessions (device ids) with one dialog stay two cards.
        for existing in self.active():
            if existing.decided_by == POLICY_DECIDER:
                continue  # auto-answered, in flight: never a dedup target
            if (existing.runtime == req.runtime and existing.kind == req.kind
                    and _device_id(existing) == _device_id(req)):
                if req.guard and req.guard != existing.guard:
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
        req = self.store.load(reply.hitl_id)
        if req is None:
            from .schema import MalformedActionError

            raise MalformedActionError(f"no requirement {reply.hitl_id}")
        action = validate_action(req, reply)
        req.chosen_action = action.id
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
        req = self.store.load(req_id)
        if req is None:
            return []
        if req.status in TERMINAL_STATUSES:
            return []
        req.transition(status)
        self.store.save(req)
        return list(req.blocked_task_ids)

    def link_blocked_task(self, req_id: str, task_id: str) -> None:
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


def _req_from_dict(d: Dict) -> HumanRequirement:
    d = dict(d)
    d["actions"] = [Action(**a) for a in d.get("actions", [])]
    return HumanRequirement(**d)
