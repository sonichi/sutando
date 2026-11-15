#!/usr/bin/env python3
"""pool_ask over s.space: the core hands a worker that has its own enabled agent identity a typed
hand-off (src/handoff_contract.py) in a room both agents are in, and collects the reply by task id.

Off unless the worker-pool manifest's WORKER_POOL_SSPACE_HANDOFF is on (CLI > env > manifest). Then
`route_for` answers a route only for an ask from the core, at team tier, to a worker whose identity
row is enabled; every other ask keeps `deliveries/<worker>/`. The room transport is the CLI at
WORKER_POOL_HANDOFF_ROOM_CLI (the consult skill's verbs: `mention`, `read`), run as a subprocess.

A hand-off arrives at the worker as a message from the core's agent, at the tier the room gives
that agent; this module grants no authority. Records: <workspace>/state/pool-handoff/<task id>.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import handoff_contract as hc, skill_config  # noqa: E401,E402

MANIFEST = _HERE.parent / "manifest.json"
FLAG = "WORKER_POOL_SSPACE_HANDOFF"
ROOM = "WORKER_POOL_HANDOFF_ROOM"
ROOM_CLI = "WORKER_POOL_HANDOFF_ROOM_CLI"
READ_LIMIT = 50
POLL_S = 5.0
CALL_TIMEOUT_S = 60


class HandoffError(ValueError):
    """The hand-off was refused or its reply could not be read; pool_ask reports it and exits 2."""


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
        raise HandoffError("s.space hand-off is on but cannot be addressed: no " + ", no ".join(missing))
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


def record_path(workspace, task_id: str) -> Path:
    return Path(workspace) / "state" / "pool-handoff" / f"{hc._check_id(task_id)}.json"


def _write_record(workspace, record: dict) -> None:
    path = record_path(workspace, record["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_record(workspace, task_id: str) -> dict:
    try:
        return json.loads(record_path(workspace, task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HandoffError(f"no s.space hand-off {task_id!r} recorded here") from e


def send(workspace, route: dict, task_id: str, worker: str, question: str, *, runner=None) -> dict:
    """Post the hand-off once and record it. An unclear outcome is recorded `unconfirmed` and never
    posted again; a refusal made before sending raises and records nothing."""
    runner = runner or _default_runner
    body = hc.build_request(task_id, question)
    record = {"task_id": task_id, "worker": worker, "room": route["room"], "room_cli": route["room_cli"],
              "to_agent": route["to_agent"], "from_agent": route["from_agent"], "event_id": None,
              "status": "sent", "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out = _call(runner, route, ["mention", route["to_agent"], body, route["room"]])
    if isinstance(out, dict) and out.get("ok") is False and out.get("posted") is False:
        raise HandoffError(f"hand-off not sent: {out.get('reason') or 'the room CLI refused'}")
    if isinstance(out, dict) and out.get("ok") and out.get("event_id"):
        record["event_id"] = out["event_id"]
    else:
        record["status"] = "unconfirmed"
    _write_record(workspace, record)
    return record


def collect(workspace, task_id: str, wait_s: float, *, runner=None, sleep=time.sleep,
            clock=time.monotonic) -> dict:
    """{status: done|failed|waiting, reply}. `waiting` at the deadline is pool_ask's timeout; a room
    no read could reach by then raises HandoffError, so a dead transport is not reported as silence."""
    runner = runner or _default_runner
    rec = read_record(workspace, task_id)
    route = {k: rec[k] for k in ("room", "room_cli", "to_agent", "from_agent")}
    deadline = clock() + max(0.0, wait_s)
    last_ok = False
    while True:
        out = _call(runner, route, ["read", rec["room"], "--limit", str(READ_LIMIT)])
        last_ok = bool(isinstance(out, dict) and out.get("ok"))
        if last_ok:
            hit = hc.find_reply(out.get("messages") or [], task_id=task_id, agent=rec["to_agent"],
                                event_id=rec.get("event_id"))
            if hit is not None:
                return {"status": hit["status"], "reply": hit["text"], "event_id": hit["event_id"]}
        if clock() >= deadline:
            if not last_ok:
                raise HandoffError(f"the hand-off room could not be read for {task_id}")
            return {"status": "waiting", "reply": None}
        sleep(min(POLL_S, max(0.01, deadline - clock())))
