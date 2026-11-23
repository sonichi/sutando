"""Recovery actions on a blocked core's or worker seat's card: the C2 action-id allowlist, the
hand-off file and the C3 receipt (contracts/core_recovery.v1/).

A recovery click is not a keystroke: the reply handler drops it to
<workspace>/state/recovery/actions/<hitl_id>.json and the monitor watching that card's session
runs it (src/recovery_actions.py). Only ids this module parses are ever written or run.
`buy_credits` is the one purchase action, tagged with its own kind so a client that must not
show a purchase (iOS) can drop exactly it; no other action or card text names a purchase.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ACTION_KIND = "recovery"
PURCHASE_KIND = "purchase"
RETRY_NOW = "retry_now"
BUY_CREDITS = "buy_credits"
ADD_ACCOUNT = "add_account"
SWITCH_ACCOUNT = "switch_account"
SWITCH_MODEL = "switch_model"
SWITCH_RUNTIME = "switch_runtime"
RESTART_CORE = "restart_core"
ESCAPE_PROMPT = "escape_prompt"
REFRESH_OPTIONS = "refresh_options"
# A model switch names an opaque token of one read of the runtime's own list (runtime_models),
# never a model or a label; which tokens exist is that list's, checked before anything runs.
_MODEL_KEY = re.compile(r"o[0-9a-f]{12}")
# A runtime is named by a slug here; which runtimes exist is the engine's list
# (core_recovery_state.RUNTIMES), checked before anything runs.
_RUNTIME_ID = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_BARE = frozenset({RETRY_NOW, RESTART_CORE, ESCAPE_PROMPT, BUY_CREDITS, ADD_ACCOUNT, REFRESH_OPTIONS})
# An account is named by its opaque digest (core_recovery_state's), never an address or token.
_ACCOUNT_ID = re.compile(r"^acct_[0-9a-f]{6}$")
# Act on the core itself, so a worker seat's card never carries them.
CORE_ONLY = frozenset({RESTART_CORE, SWITCH_RUNTIME})
PROGRESS_OUTCOMES = ("accepted", "executing")
TERMINAL_OUTCOMES = ("done", "refused", "stale", "failed")
OUTCOMES = PROGRESS_OUTCOMES + TERMINAL_OUTCOMES
RECEIPT_VERSION = 1
UPSTREAM_RECEIPTS_MAX_BYTES = 4096
AUDIT_FILE = "audit.jsonl"
AUDIT_KEEP = 500
#: Every audit writer (bridge and executor, src and vendored) holds this sidecar's flock.
AUDIT_LOCK = "audit.lock"
#: A non-owner's tap: recorded once per (card, actor), never run.
NOT_OWNER = "not_owner"
#: What audit_not_owner did: wrote the row, found it already there, or could not write.
RECORDED, DUPLICATE, NOT_RECORDED = "recorded", "duplicate", "not_recorded"

# The one sentence every surface uses about a worker's model (card, notice, reply, refusal).
SEAT_MODEL_LOCKED = "Its model can't be switched yet: workers share the core's model setting."

_LABELS = {REFRESH_OPTIONS: "Refresh options", RETRY_NOW: "Retry now", RESTART_CORE: "Restart core", ESCAPE_PROMPT: "Escape and retry",
           BUY_CREDITS: "Add usage credits", ADD_ACCOUNT: "Add another account"}


def parse(action_id: Any) -> Optional[Tuple[str, Optional[str]]]:
    """(verb, argument) for an allowlisted C2 action id, else None."""
    if not isinstance(action_id, str):
        return None
    if action_id in _BARE:
        return action_id, None
    verb, sep, arg = action_id.partition(":")
    if verb == SWITCH_MODEL and sep and _MODEL_KEY.fullmatch(arg):
        return verb, arg
    if verb == SWITCH_RUNTIME and sep and _RUNTIME_ID.fullmatch(arg):
        return verb, arg
    if verb == SWITCH_ACCOUNT and sep and _ACCOUNT_ID.fullmatch(arg):
        return verb, arg
    return None


def is_recovery(action_id: Any) -> bool:
    return parse(action_id) is not None


def kind_of(aid: str) -> str:
    """The card action kind for an allowlisted id: the purchase is tagged apart from the rest."""
    return PURCHASE_KIND if aid == BUY_CREDITS else ACTION_KIND


def action_id(verb: str, arg: Optional[str] = None) -> str:
    aid = f"{verb}:{arg}" if arg else verb
    if parse(aid) is None:
        raise ValueError(f"not a recovery action: {aid!r}")
    return aid


def label(aid: str, runtime_names: Optional[dict] = None, model_names: Optional[dict] = None) -> str:
    """The button text. `model_names` maps a model key to the runtime's own label for it."""
    parsed = parse(aid)
    if parsed is None:
        raise ValueError(f"not a recovery action: {aid!r}")
    verb, arg = parsed
    if verb == SWITCH_MODEL:
        name = (model_names or {}).get(arg)  # the token itself is never shown
        return f"Switch to {name}" if name else "Switch model"
    if verb == SWITCH_RUNTIME:
        return f"Switch to {(runtime_names or {}).get(arg, arg)}"
    if verb == SWITCH_ACCOUNT:
        return f"Switch to account {arg}"
    return _LABELS[verb]


def recovery_dir(workspace: Optional[Path] = None) -> Path:
    """Where recovery clicks, receipts and the audit live. With no workspace the repo's resolver
    is imported here, as the hitl store does, so a vendored copy never needs it."""
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    return Path(workspace) / "state" / "recovery"


def actions_dir(workspace: Path) -> Path:
    return recovery_dir(workspace) / "actions"


def receipts_dir(workspace: Path) -> Path:
    return recovery_dir(workspace) / "receipts"


def _put(path: Path, body: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".recovery-", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def write_action(workspace: Path, reply: Any, sender: str, room: str,
                 now: Optional[float] = None, tapped_at: Optional[float] = None) -> Path:
    """Atomically hand an accepted click (an ActionReply) to the host executor. `tapped_at` is the
    event's own time, when it carries one; the executor's freshness counts from the earlier stamp."""
    if not is_recovery(reply.action_id):
        raise ValueError(f"not a recovery action: {reply.action_id!r}")
    body = {"v": RECEIPT_VERSION, "hitl_id": reply.hitl_id,
            "expected_revision": reply.expected_revision, "action_id": reply.action_id,
            "guard": reply.guard, "sender": sender, "room": room,
            "at": int(time.time() if now is None else now)}
    if tapped_at is not None:
        body["tapped_at"] = int(tapped_at)
    path = _put(actions_dir(workspace) / f"{_safe(reply.hitl_id)}.json", body)
    write_progress(workspace, receipt(reply.hitl_id, "", reply.action_id,
                                     "accepted", None, body["at"]))
    return path


def event_time(event: Dict[str, Any]) -> Optional[float]:
    """The event's own send time (Matrix `origin_server_ts`, in ms), else None."""
    ts = event.get("origin_server_ts") if isinstance(event, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts <= 0:
        return None
    return ts / 1000.0


def receipt(hitl_id: str, incident: str, aid: str, outcome: str, reason: Optional[str],
            at: float, recovery: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A C3 receipt; `reason` is one plain sentence for the owner, None when done needs none."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome!r}")
    return {"v": RECEIPT_VERSION, "hitl_id": hitl_id, "incident": incident, "action_id": aid,
            "outcome": outcome, "reason": reason, "at": int(at), "recovery": dict(recovery or {})}


def write_receipt(workspace: Path, body: Dict[str, Any]) -> Path:
    if body.get("outcome") not in OUTCOMES:
        raise ValueError(f"unknown outcome: {body.get('outcome')!r}")
    return _put(receipts_dir(workspace) / f"{_safe(str(body['hitl_id']))}.json", body)


def write_progress(workspace: Path, body: Dict[str, Any]) -> None:
    """A progress receipt is advisory; losing it must not lose or delay the accepted action."""
    try:
        write_receipt(workspace, body)
    except (OSError, ValueError, KeyError):
        pass


def read_receipt(workspace: Path, hitl_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((receipts_dir(workspace) / f"{_safe(hitl_id)}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def terminal_receipt(body: Optional[Dict[str, Any]]) -> bool:
    return isinstance(body, dict) and body.get("outcome") in TERMINAL_OUTCOMES


def _upstream_receipt(value: Any) -> Optional[Dict[str, Any]]:
    """The receipt fields safe to leave the host; sender, room and unknown fields never ride."""
    if not isinstance(value, dict) or value.get("v") != RECEIPT_VERSION:
        return None
    hitl_id, aid, outcome = value.get("hitl_id"), value.get("action_id"), value.get("outcome")
    if not isinstance(hitl_id, str) or parse(aid) is None or outcome not in OUTCOMES:
        return None
    try:
        _safe(hitl_id)
    except ValueError:
        return None
    incident = value.get("incident")
    if not isinstance(incident, str) or (incident and not re.fullmatch(r"inc_[0-9a-f]{8}", incident)):
        return None
    at = value.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
        return None
    reason = value.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.isprintable() or len(reason) > 240):
        reason = None
    raw_recovery = value.get("recovery")
    safe_recovery: Dict[str, Any] = {}
    if isinstance(raw_recovery, dict):
        for key in ("state", "runtime", "model", "path"):
            field = raw_recovery.get(key)
            if isinstance(field, str) and field.isprintable() and len(field) <= 64:
                safe_recovery[key] = field
    return {"v": RECEIPT_VERSION, "hitl_id": hitl_id, "incident": incident,
            "action_id": aid, "outcome": outcome, "reason": reason, "at": int(at),
            "recovery": safe_recovery}


def latest_receipts(workspace: Path, limit: int = 16) -> list:
    """Newest bounded receipt per card, already redacted for the authenticated heartbeat."""
    out = []
    directory = receipts_dir(workspace)
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            clean = _upstream_receipt(json.loads(path.read_text()))
        except (OSError, ValueError):
            clean = None
        if clean is not None:
            out.append(clean)
    out.sort(key=lambda row: (row["at"], row["hitl_id"]), reverse=True)
    bounded = []
    for row in out[:max(0, min(int(limit), 16))]:
        candidate = [*bounded, row]
        if len(json.dumps(candidate)) > UPSTREAM_RECEIPTS_MAX_BYTES:
            break
        bounded = candidate
    return bounded


def _audit_locked(path: Path):
    """The platform-aware lock, imported here so importing this module never needs it."""
    try:
        from ..file_lock import locked_file   # the vendored package layout
    except ImportError:
        from file_lock import locked_file     # the repo's src/ on sys.path
    return locked_file(path.with_name(AUDIT_LOCK))


def _append(path: Path, entry: Dict[str, Any]) -> None:
    """Append one row, then trim to the newest AUDIT_KEEP past twice that; the caller holds the lock.
    A failed trim leaves the appended row in place and is only logged."""
    with open(path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    tmp = path.with_suffix(".tmp")
    try:
        lines = path.read_text().splitlines()
        if len(lines) > 2 * AUDIT_KEEP:
            tmp.write_text("\n".join(lines[-AUDIT_KEEP:]) + "\n")
            os.replace(tmp, path)
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).warning("recovery audit not trimmed: %s", e)
        with contextlib.suppress(OSError):
            tmp.unlink()


def audit(workspace: Path, entry: Dict[str, Any]) -> bool:
    """One line per action or refusal, bounded; ids and outcomes only, never message text.
    True when the line was written; a failure is swallowed (the receipt and card are the record)."""
    path = recovery_dir(workspace) / AUDIT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_locked(path):
            _append(path, entry)
        return True
    except (OSError, ValueError):
        return False


def audit_not_owner(workspace: Path, hitl_id: str, action_id: str, sender: str, room: str,
                    at: Optional[float] = None) -> str:
    """Record a non-owner's tap on a card, in the card's room, as `refused`/`not_owner`, once per
    (hitl_id, sender). -> RECORDED, DUPLICATE (a repeated tap adds no line) or NOT_RECORDED."""
    path = recovery_dir(workspace) / AUDIT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_locked(path):
            try:
                lines = path.read_text().splitlines()
            except FileNotFoundError:
                lines = []
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and row.get("reason") == NOT_OWNER
                        and row.get("hitl_id") == hitl_id and row.get("sender") == sender):
                    return DUPLICATE
            _append(path, {"at": int(time.time() if at is None else at), "hitl_id": hitl_id,
                           "action_id": action_id, "outcome": "refused", "reason": NOT_OWNER,
                           "sender": sender, "room": room})
        return RECORDED
    except (OSError, ValueError):
        return NOT_RECORDED


def _safe(hitl_id: str) -> str:
    """A file stem from an id: one path component, never a traversal."""
    stem = str(hitl_id)
    if not stem or not all(c.isascii() and (c.isalnum() or c in "_-") for c in stem):
        raise ValueError(f"unusable hitl id: {hitl_id!r}")
    return stem
