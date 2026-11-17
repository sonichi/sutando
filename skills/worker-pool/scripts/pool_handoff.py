#!/usr/bin/env python3
"""pool_ask over s.space: the core posts one ordinary message, addressed to a worker that has its
own enabled agent identity, in a room both agents are in. Nothing here waits for or reads a reply:
the worker answers in that message's thread, and the core's gateway delivers the answer as a task.

Off unless the worker-pool manifest's WORKER_POOL_SSPACE_HANDOFF is on (CLI > env > manifest). Then
`route_for` answers a route only for an ask from the core, at team tier, to a worker whose identity
row is enabled; every other ask keeps `deliveries/<worker>/`. A worker on its own task folders
(`connections.worker_dirs`) has no `deliveries/` path: the core's team-tier ask to it always takes this
route, flag or not, and any other ask to it is refused. The room transport is the CLI at
WORKER_POOL_HANDOFF_ROOM_CLI (the consult skill's `mention` verb), run as a subprocess.

A hand-off arrives at the worker as a message from the core's agent, at the tier the room gives
that agent; this module grants no authority. Each posted ask is one line of
<workspace>/state/pool-handoff-asks.jsonl, so `asked` can list the asks no core task has answered:
a reply is a gateway task whose `reply_to_event` is the ask's event, and its `user_id` is the
sender, compared with the agent that was asked.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import local_task_protocol, skill_config, task_lifecycle  # noqa: E401,E402

MANIFEST = _HERE.parent / "manifest.json"
FLAG = "WORKER_POOL_SSPACE_HANDOFF"
ROOM = "WORKER_POOL_HANDOFF_ROOM"
ROOM_CLI = "WORKER_POOL_HANDOFF_ROOM_CLI"
CALL_TIMEOUT_S = 60
EXCERPT = 200
RECENT_S = 7 * 86400


class HandoffError(ValueError):
    """The hand-off was refused before it was posted; pool_ask reports it and exits 2."""


def enabled(environ=None) -> bool:
    return skill_config.enabled(FLAG, MANIFEST, environ=environ)


def _rows(workspace):
    import connections
    return connections.read_rows(Path(workspace))


def own_dirs(workspace, worker: str) -> bool:
    """Whether `worker` keeps its own task folders (`connections.worker_dirs`)."""
    import connections
    return connections.worker_dirs(worker, Path(workspace)) is not None


def route_for(workspace, worker: str, *, sender: str, core: str, tier: str = "team",
              relayed_from=None, parent_task=None, environ=None, rows=None) -> "dict | None":
    """The s.space route for this ask, or None for the folder path. Raises HandoffError when the
    worker has its own identity but the hand-off cannot be addressed: never a silent fallback. A worker
    on its own folders (`own_dirs`) has no folder path, so it is asked this way or refused."""
    own = own_dirs(workspace, worker) if worker != core else False
    if not own and not enabled(environ):
        return None
    # Only the core delegates this way; relayed or parent-held asks keep the folder's tier and hold.
    if sender != core or worker == core or tier != "team" or relayed_from is not None or parent_task:
        if own:
            raise HandoffError(f"worker {worker} keeps its own folders, so only the core's own team-tier "
                               "ask reaches it (over s.space); this ask cannot")
        return None
    rows = _rows(workspace) if rows is None else rows
    mine = next((r for r in rows if r.get("worker") == worker), None)
    if mine is None or mine.get("disabled") or mine.get("parked"):
        if own:
            raise HandoffError(f"worker {worker} keeps its own folders but its identity row is not enabled")
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


def asks_path(workspace) -> Path:
    return Path(workspace) / "state" / "pool-handoff-asks.jsonl"


def record_ask(workspace, route: dict, task_id: str, worker: str, sent: dict, question: str,
               *, now=None) -> dict:
    """Append one line for a posted ask: the asking side's only trace of it."""
    rec = {"task_id": task_id, "worker": worker, "agent": route["to_agent"], "room": route["room"],
           "event_id": sent.get("event_id"), "status": sent.get("status"),
           "sent_at": datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%SZ"),
           "question": " ".join((question or "").split())[:EXCERPT]}
    path = asks_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    task_lifecycle.append_durable(path, json.dumps(rec, sort_keys=True) + "\n")
    return rec


def _epoch(stamp) -> "float | None":
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _read_asks(workspace, since: float) -> list:
    try:
        lines = asks_path(workspace).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        at = _epoch(rec.get("sent_at")) if isinstance(rec, dict) else None
        if at is not None and at >= since:
            out.append(rec)
    return out


def _task_files(tasks: Path, since: float):
    """Live and archived task files; month folders older than `since` are skipped."""
    month = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m")
    dirs = [tasks, tasks / "archive"]
    archive = tasks / "archive"
    if archive.is_dir():
        dirs += sorted(d for d in archive.iterdir() if d.is_dir() and d.name >= month)
    for d in dirs:
        yield from d.glob("task-*.txt")


def replies_by_event(workspace, since: float) -> dict:
    """{event id: [{task, sender}]} from core tasks the gateway wrote as replies to an event."""
    out: dict = {}
    for f in _task_files(Path(workspace) / "tasks", since):
        try:
            if f.stat().st_mtime < since:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Only the gateway's own layout, declared above task:, may be read past the body.
        if (local_task_protocol.parse_task_headers(text).headers.get("task_layout") or "").strip() != "mid":
            continue
        h = local_task_protocol.parse_task_headers_trusted(text).headers
        event = (h.get("reply_to_event") or "").strip()
        if event:
            out.setdefault(event, []).append({"task": (h.get("id") or f.stem).strip(),
                                              "sender": (h.get("user_id") or "").strip()})
    return out


def asked(workspace, *, now=None, window_s: float = RECENT_S) -> list:
    """Recent asks with no reply from the agent that was asked, newest first. `other_replies` names
    replies to the same event from anyone else: those are not the asked worker's answer."""
    now = time.time() if now is None else now
    asks = _read_asks(workspace, now - window_s)
    if not asks:
        return []
    replies = replies_by_event(workspace, min(_epoch(a["sent_at"]) for a in asks) - 60)
    out = []
    for a in asks:
        got = replies.get(a.get("event_id") or "", [])
        if any(g["sender"] == a.get("agent") for g in got):
            continue
        out.append({**a, "other_replies": [g for g in got if g["sender"] != a.get("agent")]})
    return sorted(out, key=lambda a: a["sent_at"], reverse=True)
