"""Recovery actions on a blocked core's or worker seat's card: the C2 action-id allowlist, the
hand-off file and the C3 receipt (contracts/core_recovery.v1/).

A recovery click is not a keystroke: the reply handler drops it to
<workspace>/state/recovery/actions/<hitl_id>.json and the monitor watching that card's session
runs it (src/recovery_actions.py). Only ids this module parses are ever written or run.
`buy_credits` is the one purchase action, tagged with its own kind so a client that must not
show a purchase (iOS) can drop exactly it; no other action or card text names a purchase.
"""

from __future__ import annotations

import json
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
# The model aliases scripts/switch-model.sh accepts that a card may offer; Fable is a spend decision.
SWITCH_ALIASES = ("opus", "sonnet", "haiku")
# A runtime is named by a slug here; which runtimes exist is the engine's list
# (core_recovery_state.RUNTIMES), checked before anything runs.
_RUNTIME_ID = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_BARE = frozenset({RETRY_NOW, RESTART_CORE, ESCAPE_PROMPT, BUY_CREDITS, ADD_ACCOUNT})
# An account is named by its opaque digest (core_recovery_state's), never an address or token.
_ACCOUNT_ID = re.compile(r"^acct_[0-9a-f]{6}$")
# Act on the core itself, so a worker seat's card never carries them.
CORE_ONLY = frozenset({RESTART_CORE, SWITCH_RUNTIME})
OUTCOMES = ("done", "refused", "stale", "failed")
RECEIPT_VERSION = 1

# The one sentence every surface uses about a worker's model (card, notice, reply, refusal).
SEAT_MODEL_LOCKED = "Its model can't be switched yet: workers share the core's model setting."

_LABELS = {RETRY_NOW: "Retry now", RESTART_CORE: "Restart core", ESCAPE_PROMPT: "Escape and retry",
           BUY_CREDITS: "Add usage credits", ADD_ACCOUNT: "Add another account"}


def parse(action_id: Any) -> Optional[Tuple[str, Optional[str]]]:
    """(verb, argument) for an allowlisted C2 action id, else None."""
    if not isinstance(action_id, str):
        return None
    if action_id in _BARE:
        return action_id, None
    verb, sep, arg = action_id.partition(":")
    if verb == SWITCH_MODEL and sep and arg in SWITCH_ALIASES:
        return verb, arg
    if verb == SWITCH_RUNTIME and sep and _RUNTIME_ID.match(arg):
        return verb, arg
    if verb == SWITCH_ACCOUNT and sep and _ACCOUNT_ID.match(arg):
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
    """The button text. `model_names` maps an alias to the exact model the switch sets (the
    model-switch table), so the button names the model the switch sends."""
    parsed = parse(aid)
    if parsed is None:
        raise ValueError(f"not a recovery action: {aid!r}")
    verb, arg = parsed
    if verb == SWITCH_MODEL:
        return f"Switch to {(model_names or {}).get(arg) or arg.capitalize()}"
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
                 now: Optional[float] = None) -> Path:
    """Atomically hand an accepted click (an ActionReply) to the host executor."""
    if not is_recovery(reply.action_id):
        raise ValueError(f"not a recovery action: {reply.action_id!r}")
    body = {"v": RECEIPT_VERSION, "hitl_id": reply.hitl_id,
            "expected_revision": reply.expected_revision, "action_id": reply.action_id,
            "guard": reply.guard, "sender": sender, "room": room,
            "at": int(time.time() if now is None else now)}
    return _put(actions_dir(workspace) / f"{_safe(reply.hitl_id)}.json", body)


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


def read_receipt(workspace: Path, hitl_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((receipts_dir(workspace) / f"{_safe(hitl_id)}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _safe(hitl_id: str) -> str:
    """A file stem from an id: one path component, never a traversal."""
    stem = "".join(c for c in str(hitl_id) if c.isalnum() or c in "_-")
    if not stem:
        raise ValueError(f"unusable hitl id: {hitl_id!r}")
    return stem
