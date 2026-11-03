"""The claude-local card: a HumanRequirement in its own store root. The bridge owns
the projector and the accepter for that root, so this lives outside the adapter, which imports it.

Requirements live in `hitl/requirements-claude-local` beside the bridge's default store, which an
unmodified gateway never reads; ids are namespaced `hitl_cl_<…>`. Everything the accepter and the
reconciler need rides inside `subject["claude-local"]` (saga key, effect phase, stall token,
successor id, room, discriminator, install id, root-relative lock coordinates), which an older
writer preserves on re-save. `Keep waiting` is a successor requirement whose id and guard derive
from the persisted saga key, so a crash at any step reconciles to exactly one successor.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import claude_local_records as records
from .hitl.manager import HitlManager, HitlStore
from .hitl.schema import Action, HumanRequirement, STATUS_IN_PROGRESS, STATUS_PENDING, TERMINAL_STATUSES

HITL_ID_PREFIX = "hitl_cl_"
HITL_ROOT_NAME = "requirements-claude-local"
RUNTIME = "claude-local"
INSTALLS = "installs.json"

KIND_STALL, KIND_UNKNOWN = "claude-local.stall", "claude-local.unknown"
ACTION_STOP, ACTION_KEEP = "stop", "keep-waiting"
PHASE_STOPPING, PHASE_REARMING = "STOPPING", "REARMING"
SUBJECT = "claude-local"
COORD_PROGRESS, COORD_SUPERVISOR = "state/progress.lock", "state/claude-local-supervisor.json"
TAIL_LINES = 20
KEEP_WAITING_S = 90.0


def store_root(bridge_state_dir) -> Path:
    """The adapter's root beside the bridge's default store: <state>/hitl/requirements-claude-local."""
    return Path(bridge_state_dir) / "hitl" / HITL_ROOT_NAME


def default_root(bridge_state_dir) -> Path:
    return Path(bridge_state_dir) / "hitl" / "requirements"


def opaque_install(install_id: str, root) -> str:
    """The wire-visible install identity: opaque, no host path; one per install and work root."""
    return "cl-" + _digest("install", install_id, str(root))[:20]


def register_install(store, opaque: str, root, supervisor_lock) -> None:
    """The adapter's injection: what the bridge's accepter resolves an opaque id to (absolute paths,
    local only, never on the wire); written durably, merged with other installs."""
    path = Path(store) / INSTALLS
    with records.flocked(Path(store) / ".installs.lock"):
        current = records.read_json(path)
        current[opaque] = {"root": str(root), "supervisor_lock": str(supervisor_lock)}
        records.write_durable(path, json.dumps(current, sort_keys=True).encode("utf-8"))


def unregister_install(store, opaque: str) -> None:
    path = Path(store) / INSTALLS
    if not path.exists():
        return
    with records.flocked(Path(store) / ".installs.lock"):
        current = records.read_json(path)
        if current.pop(opaque, None) is not None:
            records.write_durable(path, json.dumps(current, sort_keys=True).encode("utf-8"))


def resolve_install(store, opaque: str) -> "tuple[str, str]":
    """(work root, supervisor lock) for a registered opaque id; ValueError for anything else."""
    entry = records.read_json(Path(store) / INSTALLS).get(str(opaque or ""))
    if not isinstance(entry, dict) or not os.path.isabs(str(entry.get("root") or "")) \
            or not os.path.isabs(str(entry.get("supervisor_lock") or "")):
        raise ValueError("install %s is not registered with this bridge" % (opaque or "?"))
    return entry["root"], entry["supervisor_lock"]


def manager(root) -> "tuple[HitlStore, HitlManager]":
    """One fresh store, and the manager constructed over that exact object (policy=None: never
    auto-decided, and the adapter's kinds have no policy entry)."""
    store = HitlStore(Path(root))
    return store, HitlManager(store, policy=None)


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def new_id(incident: str, salt: str = "") -> str:
    return HITL_ID_PREFIX + _digest("id", incident, salt or os.urandom(8).hex())[:12]


def successor_id(saga_key: dict) -> str:
    """Deterministic from the canonical saga key, so a crash never mints a second successor."""
    return HITL_ID_PREFIX + _digest("successor", saga_key)[:12]


def successor_guard(saga_key: dict) -> str:
    return _digest("guard", saga_key)[:24]


def local(req: HumanRequirement) -> dict:
    sub = req.subject.get(SUBJECT) if isinstance(req.subject, dict) else None
    return dict(sub) if isinstance(sub, dict) else {}


def coordinates_ok(sub: dict) -> bool:
    """The lock coordinates are root-relative and confined: no absolute path, no `..` component."""
    for key in ("progress_lock", "supervisor_record"):
        value = str(sub.get(key) or "")
        if not value or os.path.isabs(value) or ".." in Path(value).parts:
            return False
    return True


def resolve_under(root, rel) -> Path:
    """`rel` resolved inside `root`; ValueError when it escapes."""
    root = Path(root).resolve()
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        raise ValueError("%s escapes the install root" % rel)
    return path


def build(kind: str, *, incident: str, install_id: str, room: str, stall: dict, tail: list, attach_cmd: str,
          failed_check: str, guard: str = "", req_id: str = "", deadline: float = 0.0) -> HumanRequirement:
    """The requirement as the launcher creates it; `kind` is kept as the discriminator inside the
    subject since the schema normalizes an unknown kind on re-save."""
    token = records.stall_token(stall)
    what = "suspected hang" if kind == KIND_STALL else "needs attention"
    reason = (stall or {}).get("reason") or failed_check
    lines = "\n".join(tail[-TAIL_LINES:])
    message = ("%s: %s\nwhich check failed: %s\nattach: %s\n\nlast %d pane lines:\n%s"
               % (what, reason, failed_check, attach_cmd, TAIL_LINES, lines))
    sub = {"saga_key": None, "effect_phase": None, "stall_token": token, "successor_id": None, "room": room,
           "discriminator": kind, "install_id": install_id, "incident": incident,
           "progress_lock": COORD_PROGRESS, "supervisor_record": COORD_SUPERVISOR, "deadline": deadline}
    return HumanRequirement(kind=kind, runtime=RUNTIME, message=message, id=req_id or new_id(incident),
                            guard=guard or _digest("guard", incident, token)[:24], device={"id": install_id},
                            title="Claude Code session (local)", room=room,
                            actions=[Action(id=ACTION_STOP, kind="stop", label="Stop and resume"),
                                     Action(id=ACTION_KEEP, kind="rearm", label="Keep waiting")],
                            subject={SUBJECT: sub}, expires_at=None)


def patch_for(action_id: str, reply_revision: int, incident: str):
    """The pure, merge-preserving patch apply_action runs inside its one save: the effect phase, the
    accepted revision and (for Keep waiting) the saga key, all inside subject['claude-local']."""
    def patch(req: HumanRequirement) -> HumanRequirement:
        sub = local(req)
        sub["accepted_revision"] = reply_revision
        if action_id == ACTION_STOP:
            sub["effect_phase"] = PHASE_STOPPING
        else:
            sub["effect_phase"] = PHASE_REARMING
            sub["saga_key"] = {"incident_id": incident, "predecessor_id": req.id,
                               "accepted_expected_revision": reply_revision}
            sub["successor_id"] = successor_id(sub["saga_key"])
        req.subject = dict(req.subject, **{SUBJECT: sub})
        return req
    return patch


def reconcile_rearm(store: HitlStore, mgr: HitlManager, req: HumanRequirement, now=None) -> "str | None":
    """Idempotent, in a fixed order, each step checking the previous: the predecessor resolved, the
    successor created (or found by its id with an exact saga-key match; a same-id record with another
    key is refused), then the successor id returned for the pointer move. None when not REARMING."""
    sub = local(req)
    if sub.get("effect_phase") != PHASE_REARMING or not sub.get("saga_key"):
        return None
    saga, sid = sub["saga_key"], sub["successor_id"] or successor_id(sub["saga_key"])
    if saga.get("predecessor_id") != req.id or sid != successor_id(saga):
        raise ValueError("%s: saga key does not derive this successor" % req.id)
    with store.locked():
        existing = store.load(sid)
        if existing is not None:
            found = local(existing)
            if found.get("saga_key") != saga:
                raise ValueError("%s: a record with the successor's id carries another saga key; refused" % sid)
        if req.status == STATUS_IN_PROGRESS:
            mgr.resolve(req.id)
        if existing is None:
            successor = build(sub.get("discriminator") or KIND_STALL, incident=sub.get("incident") or "",
                              install_id=sub.get("install_id") or "", room=sub.get("room") or "",
                              stall=None, tail=req.message.split("\n")[-TAIL_LINES:], attach_cmd="",
                              failed_check="rearmed", guard=successor_guard(saga), req_id=sid,
                              deadline=(now or time.time()) + KEEP_WAITING_S)
            succ = local(successor)
            succ.update({"saga_key": saga, "stall_token": sub.get("stall_token"), "predecessor_id": req.id})
            successor.subject = {SUBJECT: succ}
            successor.message = req.message
            created = mgr.create(successor)
            if created.id != sid:
                raise ValueError("%s: create collapsed the successor onto %s" % (sid, created.id))
    return sid


def open_actions(store: HitlStore) -> list:
    """The pending or in-progress claude-local requirements (ids namespaced hitl_cl_)."""
    return [r for r in store.all() if r.status not in TERMINAL_STATUSES and r.id.startswith(HITL_ID_PREFIX)]


def pending(store: HitlStore) -> list:
    return [r for r in store.all() if r.status == STATUS_PENDING]
