#!/usr/bin/env python3
"""observe_policy — the deterministic core of /observe (Policy Interaction
Layer MVP, step 1). Owner-framed north star: /observe is the natural-language
entry to the Policy Engine — NL in, a structured subscription policy out, with
human confirmation (or a standing approval) in between.

Layering (design: workspace notes/observe-mvp-slice-design.md, all four open
questions resolved 2026-07-24):
  - The NL→draft COMPILATION is the core agent's job (LLM-side — see SKILL.md);
    this module is everything deterministic around it:
      validate_draft   — schema + value discipline for what the compiler emits
      evaluate_standing_approval — the built-in standing approval: auto-activate
                         ONLY inside a locked scope, NEVER silently
      SubscriptionStore — file-per-record store under <workspace>/state/observe/
      render_card      — the effect-card (plain text + optional A2UI
                         choice-group in the REAL renderer contract)
  - The Evaluator for room-level permission stays the events plane's four-way
    authz (grant + membership + PL>=50 + room policy) — enforced server-side at
    subscribe time; this module never re-implements it.

Standing approval (hard conditions, resolved):
  (a) VISIBILITY — an auto-activated policy always announces itself; the card
      text carries "auto-activated per your standing approval". Never silent.
  (b) SCOPE LOCKED — created_by == owner, room in owner-scoped rooms,
      mode in {observe, record, notify}, no privileged actions (notify-only),
      cost cap <= the default. Anything outside -> explicit card.

Cost line shows the CAP only ("<= N evals/day (default cap)") — measured usage
is a later slice (needs metering).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
import uuid

MODES = frozenset({"observe", "record", "notify", "taskify"})
STANDING_MODES = frozenset({"observe", "record", "notify"})  # taskify = explicit card
DEFAULT_EVALS_PER_DAY = 2
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
# policy_id is LLM-adjacent input (a compiled draft can carry one, and the
# decision grammar echoes them back) — it is ALSO a filename component, so the
# accepted shape is exactly what we generate: obs_ + hex. Anything else is
# discarded/refused before it can touch a path (review P1: `../core-status`
# escaped state/observe/ and could overwrite arbitrary workspace JSON).
_POLICY_ID_RE = re.compile(r"^obs_[0-9a-f]{8,32}$")


def validate_draft(draft: dict) -> "tuple[dict, list[str]]":
    """Validate + normalize a compiler-emitted draft. Returns (normalized,
    errors). Empty errors == valid. Unknown keys are dropped (the compiler is
    an LLM — schema discipline lives HERE, not in the prompt)."""
    errors: list[str] = []
    room_id = str(draft.get("room_id") or "")
    if not room_id.startswith("!"):
        errors.append("room_id must be a !room id")
    types = draft.get("event_types") or []
    if not isinstance(types, list) or not types:
        errors.append("event_types must be a non-empty list")
        types = []
    bad = [t for t in types if not _EVENT_TYPE_RE.match(str(t))]
    if bad:
        errors.append(f"malformed event types: {bad}")
    mode = str(draft.get("mode") or "observe")
    if mode not in MODES:
        errors.append(f"mode must be one of {sorted(MODES)}")
    # Absent/None mean "no cap requested" (default applies, no error). Any
    # PRESENT non-dict — INCLUDING falsy ones like [] or "" — is malformed LLM
    # output and must surface as a validation error: `or {}` silently blessed
    # cost_cap=[] into a default-cap draft that standing approval then
    # auto-activated (review P1 follow-up — malformed output must force
    # explicit confirmation, never widen the standing-approval boundary).
    cap = draft.get("cost_cap")
    if cap is None:
        cap = {}
    elif not isinstance(cap, dict):
        errors.append('cost_cap must be an object like {"evals_per_day": n}')
        cap = {}
    evals = cap.get("evals_per_day", DEFAULT_EVALS_PER_DAY)
    if not isinstance(evals, int) or evals < 0:
        errors.append("cost_cap.evals_per_day must be a non-negative int")
        evals = DEFAULT_EVALS_PER_DAY
    # Keep a provided id ONLY when it already has the generated shape (edit
    # flow round-trips ids we minted); anything else gets a fresh id — an id
    # is infrastructure, not user intent, so no error, just never trusted.
    pid = str(draft.get("policy_id") or "")
    if not _POLICY_ID_RE.match(pid):
        pid = f"obs_{uuid.uuid4().hex[:12]}"
    normalized = {
        "policy_id": pid,
        "room_id": room_id,
        "event_types": [str(t) for t in types],
        "mode": mode,
        "cost_cap": {"evals_per_day": evals},
        "created_by": str(draft.get("created_by") or ""),
        "source_text": str(draft.get("source_text") or "")[:400],
        "status": "draft",
        "created_at": time.time(),
    }
    if not normalized["created_by"].startswith("@"):
        errors.append("created_by must be an mxid")
    return normalized, errors


def evaluate_standing_approval(draft: dict, *, owner_mxid: str,
                               owner_rooms: "list[str]") -> "tuple[bool, str]":
    """The single built-in standing approval. Returns (auto_activate, reason).
    Fail-closed: anything outside the locked scope -> explicit card. The
    VISIBILITY condition is enforced by the caller contract: an auto-activated
    policy MUST be announced (render_card(auto_activated=True) provides the
    text) — this function only decides, it never activates silently."""
    if not owner_mxid or draft.get("created_by") != owner_mxid:
        return False, "requester is not the owner"
    if draft.get("room_id") not in (owner_rooms or []):
        return False, "room is not in the owner's scoped rooms"
    if draft.get("mode") not in STANDING_MODES:
        return False, f"mode {draft.get('mode')!r} requires explicit confirmation"
    cap = (draft.get("cost_cap") or {}).get("evals_per_day")
    if not isinstance(cap, int) or cap > DEFAULT_EVALS_PER_DAY:
        # missing/malformed cap on an UNVALIDATED draft must deny, not crash —
        # the boundary is self-contained (001 review), it never assumes the
        # caller ran validate_draft first.
        return False, "cost cap missing or above the default — explicit confirmation required"
    return True, "within standing approval (self + scoped room + notify-only + default cap)"


class StoreLockUnavailable(RuntimeError):
    """The store lock could not be taken within its timeout — an error, never a
    silent fall-through into the critical section."""


_STORE_LOCKS: "dict[str, list]" = {}


@contextlib.contextmanager
def store_lock(store_dir: str, *, timeout: float = 10.0):
    """Process-wide re-entrant exclusive lock per store directory (flock, bounded
    wait). Not thread-aware; re-entrancy does not make nested calls atomic."""
    key = os.path.realpath(store_dir)
    held = _STORE_LOCKS.get(key)
    if held is not None and held[1] > 0:
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
        return

    os.makedirs(store_dir, exist_ok=True)
    fd = os.open(os.path.join(key, ".store.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise StoreLockUnavailable(
                    f"store lock busy for >{timeout}s: {key}")
            time.sleep(0.02)

    _STORE_LOCKS[key] = [fd, 1]
    try:
        yield
    finally:
        _STORE_LOCKS.pop(key, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class SubscriptionStore:
    """File-per-policy store — same inspectable pattern as the human-action
    store. NOT single-writer: mutating sequences run under store_lock(self.dir)."""

    def __init__(self, store_dir: str):
        self.dir = store_dir

    def _path(self, policy_id: str) -> str:
        # DEFENSE IN DEPTH at the filesystem boundary: even if a caller skips
        # validate_draft, an id outside the generated shape never becomes a
        # path (review P1 traversal), and the resolved path must stay inside
        # the store dir (belt for symlink/normalization surprises).
        pid = str(policy_id)
        if not _POLICY_ID_RE.match(pid):
            raise ValueError(f"invalid policy id shape: {pid!r}")
        path = os.path.join(self.dir, pid + ".json")
        base = os.path.realpath(self.dir)
        if os.path.dirname(os.path.realpath(path)) != base:
            raise ValueError(f"policy path escapes the store: {pid!r}")
        return path

    def save(self, rec: dict) -> str:
        os.makedirs(self.dir, exist_ok=True)
        path = self._path(rec["policy_id"])
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path

    def get(self, policy_id: str) -> "dict | None":
        try:
            with open(self._path(policy_id)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def list(self, status: "str | None" = None) -> "list[dict]":
        out = []
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return out
        for n in names:
            if not (n.startswith("obs_") and n.endswith(".json")):
                continue
            rec = self.get(n[:-5])
            if rec and (status is None or rec.get("status") == status):
                out.append(rec)
        return out

    def transition(self, policy_id: str, to: str, note: str = "") -> bool:
        """draft->active|cancelled, active->cancelled|expired; terminal states
        immutable. Read and write are one critical section under the store lock."""
        with store_lock(self.dir):
            rec = self.get(policy_id)
            if not rec:
                return False
            allowed = {"draft": {"active", "cancelled"},
                       "active": {"cancelled", "expired"}}
            if to not in allowed.get(rec.get("status", ""), set()):
                return False
            rec["status"] = to
            rec.setdefault("audit", []).append(
                {"at": time.time(), "to": to, **({"note": note} if note else {})})
            self.save(rec)
            return True


def render_card(rec: dict, *, auto_activated: bool = False,
                include_a2ui: bool = False) -> str:
    """Effect-card: what this policy DOES, its cost cap, and the choices.
    Plain markdown always (any client); optional A2UI choice-group in the REAL
    renderer contract (components/choices/action{event,field}) behind a flag —
    the same staging discipline as the human-action cards."""
    kinds = ", ".join(rec["event_types"])
    lines = []
    if auto_activated:
        lines.append("🔭 **Observation activated** — auto-activated per your "
                     "standing approval (self-scope, notify-only). Reply "
                     f"`policy {rec['policy_id']} cancel` to undo.")
    else:
        lines.append("🔭 **Confirm this observation policy**")
    lines += [
        "",
        f"• Room: {rec['room_id']}",
        f"• Events: {kinds}",
        f"• Mode: {rec['mode']} (no privileged actions)",
        f"• Cost: ≤ {rec['cost_cap']['evals_per_day']} evals/day (default cap)",
        f"• From: “{rec['source_text']}”" if rec.get("source_text") else "",
        "",
    ]
    if not auto_activated:
        lines.append(f"Reply `policy {rec['policy_id']} activate | edit <text> | cancel`.")
        if include_a2ui:
            card = {"version": "0.9", "title": "Confirm observation policy",
                    "surface": "human-action",
                    "components": [{"type": "choice-group", "label": "Decision",
                                    "choices": [
                                        {"label": lbl, "value": val,
                                         "action": {"event": "space.ag2.ha.answer",
                                                    "field": "choice"}}
                                        for lbl, val in (("✅ Activate", "activate"),
                                                         ("✏️ Edit", "edit"),
                                                         ("❌ Cancel", "cancel"))]}]}
            lines += ["", "```a2ui", json.dumps(card, ensure_ascii=False), "```"]
    return "\n".join(ln for ln in lines if ln is not None)
