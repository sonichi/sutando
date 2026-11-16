#!/usr/bin/env python3
"""pool_ask over s.space: the core posts one ordinary message, addressed to a worker that has its
own enabled agent identity, in a room both agents are in. Nothing here waits for or reads a reply:
the worker answers in that message's thread, and the core's gateway delivers the answer as a task.

Off unless the worker-pool manifest's WORKER_POOL_SSPACE_HANDOFF is on (CLI > env > manifest). Then
`route_for` answers a route only for an ask from the core, at team tier, to a worker whose identity
row is enabled; every other ask keeps `deliveries/<worker>/`. The room transport is the CLI at
WORKER_POOL_HANDOFF_ROOM_CLI (the consult skill's `mention` verb), run as a subprocess.

A hand-off arrives at the worker as a message from the core's agent, at the tier the room gives
that agent; this module grants no authority.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import skill_config  # noqa: E402

MANIFEST = _HERE.parent / "manifest.json"
FLAG = "WORKER_POOL_SSPACE_HANDOFF"
ROOM = "WORKER_POOL_HANDOFF_ROOM"
ROOM_CLI = "WORKER_POOL_HANDOFF_ROOM_CLI"
CALL_TIMEOUT_S = 60


class HandoffError(ValueError):
    """The hand-off was refused before it was posted; pool_ask reports it and exits 2."""


def enabled(environ=None) -> bool:
    return skill_config.enabled(FLAG, MANIFEST, environ=environ)


def _rows(workspace):
    import connections
    return connections.read_rows(Path(workspace))


def route_for(workspace, worker: str, *, sender: str, core: str, tier: str = "team",
              relayed_from=None, parent_task=None, environ=None, rows=None) -> "dict | None":
    """The s.space route for this ask, or None for the folder path. Raises HandoffError when the
    worker has its own identity but the hand-off cannot be addressed: never a silent fallback."""
    if not enabled(environ):
        return None
    # Only the core delegates this way; relayed or parent-held asks keep the folder's tier and hold.
    if sender != core or worker == core or tier != "team" or relayed_from is not None or parent_task:
        return None
    rows = _rows(workspace) if rows is None else rows
    mine = next((r for r in rows if r.get("worker") == worker), None)
    if mine is None or mine.get("disabled") or mine.get("parked"):
        return None
    to_agent = str(mine.get("agent_mxid") or "")
    server = str(mine.get("server") or "").lower()
    core_row = next((r for r in rows if not r.get("worker") and not r.get("disabled") and not r.get("parked")
                     and str(r.get("server") or "").lower() == server and r.get("agent_mxid")), None)
    room = skill_config.config_value(ROOM, MANIFEST, environ=environ)
    cli = skill_config.config_value(ROOM_CLI, MANIFEST, environ=environ)
    missing = [what for what, v in (("the worker's agent id", to_agent),
                                    ("an enabled core lane on the worker's server", core_row),
                                    (ROOM, room), (ROOM_CLI, cli)) if not v]
    if missing:
        raise HandoffError("room hand-off is on but cannot be addressed: no " + ", no ".join(missing))
    return {"room": room, "room_cli": cli, "to_agent": to_agent, "from_agent": str(core_row["agent_mxid"])}


def _default_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=CALL_TIMEOUT_S)


def _call(runner, route: dict, verb_args: list) -> "dict | None":
    argv = [sys.executable, route["room_cli"]] + verb_args + ["--agent", route["from_agent"]]
    try:
        r = runner(argv)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        parsed = json.loads(r.stdout or "")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_message(task_id: str, question: str) -> str:
    """The message body; the transport adds the worker's mention in front. The id is for people."""
    text = (question or "").strip()
    if not text:
        raise HandoffError("the work to hand over is empty")
    return f"{text}\n\n(pool-ask {task_id}: reply in this message's thread)"


def send(route: dict, task_id: str, question: str, *, runner=None) -> dict:
    """Post the hand-off once. {status: sent|unconfirmed, event_id}; an unclear outcome is never
    posted again, and a refusal made before sending raises."""
    runner = runner or _default_runner
    body = build_message(task_id, question)
    out = _call(runner, route, ["mention", route["to_agent"], body, route["room"]])
    if isinstance(out, dict) and out.get("ok") is False and out.get("posted") is False:
        raise HandoffError(f"hand-off not sent: {out.get('reason') or 'the room CLI refused'}")
    if isinstance(out, dict) and out.get("ok") and out.get("event_id"):
        return {"status": "sent", "event_id": out["event_id"]}
    return {"status": "unconfirmed", "event_id": None}
