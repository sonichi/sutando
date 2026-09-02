"""Ingest RuntimeEvents dropped by the runtime drivers (the desktop watchdog's
`hitl_events.rs`) into the HumanRequirement Manager.

Seam contract: one JSON file per (session, guard) under
<workspace>/state/hitl/events/, schema space.ag2.hitl.runtime_event.v1. A live
event carries kind/prompt/options/guard plus the jump target (session +
socket); a tombstone carries `cleared: true` and means the runtime stopped
waiting — the requirement is resolved if a human acted on it (in_progress) or
expired if nobody did. Ingest is idempotent: a file already reflected in the
Manager is a no-op, and consumed files are removed only after the Manager has
durably recorded them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .manager import HitlManager
from .schema import (
    STATUS_IN_PROGRESS,
    Action,
    HumanRequirement,
)

SCHEMA = "space.ag2.hitl.runtime_event.v1"
JUMP_ACTION_ID = "open_terminal"


def events_dir(workspace: Path) -> Path:
    return Path(workspace) / "state" / "hitl" / "events"


def _actions_for(ev: Dict) -> List[Action]:
    kind = ev.get("kind")
    out: List[Action] = []
    if kind == "auth":
        out.append(Action(id="reauth", kind="authenticate", label="Re-authenticate"))
    for o in ev.get("options") or []:
        oid, label = str(o.get("id", "")), str(o.get("label", ""))
        if oid and label:
            # The option id is the key the TUI expects; the driver types it.
            out.append(Action(id=oid, kind="tui_select", label=label))
    # The floor: every TUI-sourced requirement can be finished in the terminal.
    out.append(Action(id=JUMP_ACTION_ID, kind="open_terminal", label=f"Open terminal ({ev.get('session', '?')})"))
    return out


def _requirement_for(ev: Dict) -> HumanRequirement:
    session = str(ev.get("session") or "?")
    runtime = str(ev.get("runtime") or "unknown")
    prompt = ev.get("prompt") or {
        "auth": f"{runtime} needs to sign in again",
        "permission": f"{runtime} is asking permission to run a tool",
        "confirmation": f"{runtime} is waiting for your confirmation",
        "choice": f"{runtime} is asking you a question",
    }.get(ev.get("kind"), f"{runtime} is waiting on something in its terminal")
    return HumanRequirement(
        kind=str(ev.get("kind") or "unknown"),
        runtime=runtime,
        message=f"{prompt} — session {session}",
        guard=str(ev.get("guard") or ""),
        # The session is the requirement's device: it keys dedup and the jump.
        device={"id": session, "name": session},
        title=f"{runtime} · {session}",
        actions=_actions_for(ev),
    )


def ingest(manager: HitlManager, workspace: Path) -> Dict[str, int]:
    """One pass over the events dir. Returns counts for the caller's log."""
    counts = {"created": 0, "resolved": 0, "expired": 0, "skipped": 0, "bad": 0}
    d = events_dir(workspace)
    if not d.is_dir():
        return counts
    for path in sorted(d.glob("*.json")):
        try:
            ev = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            counts["bad"] += 1
            continue
        if not isinstance(ev, dict) or ev.get("schema") != SCHEMA:
            counts["bad"] += 1
            continue
        guard = str(ev.get("guard") or "")
        existing = _find_by_guard(manager, guard)
        if ev.get("cleared"):
            if existing is None:
                counts["skipped"] += 1
            elif existing.status == STATUS_IN_PROGRESS:
                manager.resolve(existing.id)
                counts["resolved"] += 1
            else:
                manager.expire(existing.id)
                counts["expired"] += 1
            path.unlink(missing_ok=True)
            continue
        if existing is not None:
            counts["skipped"] += 1  # already reflected; the file is the driver's, leave it
            continue
        manager.create(_requirement_for(ev))
        counts["created"] += 1
    return counts


def _find_by_guard(manager: HitlManager, guard: str) -> Optional[HumanRequirement]:
    if not guard:
        return None
    for req in manager.store.all():
        if req.guard == guard and not req.terminal:
            return req
    return None
