#!/usr/bin/env python3
"""owner-agent-consult CLI — ask another agent of the same owner, in the owner-only
consult room, before answering the owner. Prints one JSON object; refusals are in-band.

  consult.py roster [--agent SELF] [--room ROOM]
  consult.py ask --agent-to MXID --domain TEXT --question-file F --task-file T
                 [--agent SELF] [--room ROOM] [--max-wait S]

Transport is the agent-room-ops skill (its members/read/mention verbs and the
/v1/agents registry); the policy lives in consult_policy.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import consult_policy as policy  # noqa: E402

ROOM_OPS_DIR = Path(__file__).resolve().parents[2] / "agent-room-ops"
CI_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "collaboration-intelligence" / "scripts"


class RoomOpsTransport:
    """The four calls the policy needs, each delegated to the room-ops module that owns it."""

    def __init__(self, self_mxid: Optional[str]):
        if not (ROOM_OPS_DIR / "room_ops.py").is_file():
            raise RuntimeError("agent-room-ops skill is not installed")
        if str(ROOM_OPS_DIR) not in sys.path:
            sys.path.insert(0, str(ROOM_OPS_DIR))
        import members as _members
        import mention as _mention
        import read as _read
        import resolve as _resolve
        self._m, self._mention, self._read, self._resolve = _members, _mention, _read, _resolve
        self.self_mxid = self_mxid

    def members(self, room):
        return self._m.room_members(room, self.self_mxid)

    def agents(self):
        return self._resolve.list_agents()

    def mention(self, mxid, body, room):
        return self._mention.mention(mxid, body, room, self.self_mxid)

    def read(self, room, limit):
        return self._read.read_room(room, self.self_mxid, limit)


def load_map(workspace: Path):
    """(entities, quick_lookup) from the collaboration-intelligence store; empty when absent."""
    if not (CI_SCRIPTS_DIR / "lookup.py").is_file():
        return [], {}
    if str(CI_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(CI_SCRIPTS_DIR))
    import lookup
    quick, ents = lookup.load(workspace / "data" / "collaboration-intelligence")
    return ents or [], quick or {}


def _workspace() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def _emit(obj) -> int:
    print(json.dumps(obj, indent=2, default=sorted))
    return 0


def main(argv=None, transport=None, workspace: Optional[Path] = None) -> int:
    p = argparse.ArgumentParser(prog="consult.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("roster", "ask"):
        s = sub.add_parser(name)
        s.add_argument("--agent", dest="self_mxid", default=os.environ.get("AGENT_MXID"),
                       help="this agent's own mxid (room-ops convention)")
        s.add_argument("--room", default=None, help=f"overrides {policy.CONFIG_ROOM}")
        if name == "ask":
            s.add_argument("--agent-to", required=True, help="an mxid from `roster`")
            s.add_argument("--domain", required=True, help="what the map says that agent holds")
            s.add_argument("--question-file", required=True)
            s.add_argument("--task-file", required=True)
            s.add_argument("--max-wait", default=None, help=f"overrides {policy.CONFIG_MAX_WAIT}")
    a = p.parse_args(argv)

    conf = policy.settings(room=a.room, max_wait=getattr(a, "max_wait", None))
    if not conf["active"]:
        return _emit({"ok": False, "inert": True, "answered": False, "reason": conf["reason"]})
    try:
        transport = transport or RoomOpsTransport(a.self_mxid)
    except RuntimeError as e:
        return _emit({"ok": False, "answered": False, "reason": str(e)})
    ws = workspace or _workspace()
    ents, quick = load_map(ws)

    if a.cmd == "roster":
        verdict = policy.guard(conf["room"], a.self_mxid or "", transport)
        if not verdict["ok"]:
            return _emit({"ok": False, "reason": verdict["reason"]})
        return _emit({"ok": True, "agents": policy.roster(verdict, a.self_mxid, ents, quick)})

    try:
        question = Path(a.question_file).read_text(encoding="utf-8")
        task_text = Path(a.task_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _emit({"ok": False, "answered": False, "reason": f"unreadable input: {e}"})
    res = policy.consult(transport, room=conf["room"], self_mxid=a.self_mxid or "",
                         agent=a.agent_to, domain=a.domain, question=question,
                         task_text=task_text, max_wait_s=conf["max_wait_s"],
                         ents=ents, quick=quick, workspace=ws)
    return _emit({"ok": True, **res})


if __name__ == "__main__":
    raise SystemExit(main())
