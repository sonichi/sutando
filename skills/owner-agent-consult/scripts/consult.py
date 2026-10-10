#!/usr/bin/env python3
"""owner-agent-consult CLI — ask another agent of the same owner, in the owner-only
consult room, and later match its reply (which arrives as a new task) back to the owner's
task. Prints one JSON object; refusals are in-band.

  consult.py roster  [--agent SELF] [--room ROOM] [--room-cli CLI]
  consult.py ask     --agent-to MXID --question-file F --task-id OWNER_TASK_ID [--agent SELF] ...
  consult.py match   --task-id REPLY_TASK_ID [--agent SELF] ...
  consult.py pending [--nudged CID] [--nudge-after S]

The room transport is the CLI named by OWNER_AGENT_CONSULT_ROOM_CLI, run as a subprocess
through its public verbs (agents, members, read, mention); the policy lives in consult_policy.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import consult_policy as policy  # noqa: E402

CLI_TIMEOUT_S = 60


class RoomCliTransport:
    """The four calls the policy needs, each one run of the configured room CLI."""

    def __init__(self, cli_path: str, self_mxid: Optional[str], runner=subprocess.run):
        path = Path(cli_path)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError(f"room transport CLI not found ({policy.CONFIG_ROOM_CLI} must be an absolute path)")
        self.cli, self.self_mxid, self._run = str(path), self_mxid or "", runner

    def _call(self, *args) -> dict:
        argv = [sys.executable, self.cli, *args]
        try:
            p = self._run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "reason": f"room transport failed: {e}"}
        try:
            res = json.loads(p.stdout)
        except (TypeError, ValueError):
            return {"ok": False, "reason": f"room transport printed no JSON (exit {p.returncode})"}
        return res if isinstance(res, dict) else {"ok": False, "reason": "room transport output is not an object"}

    def agents(self):
        return self._call("agents")

    def members(self, room):
        return self._call("members", room, "--agent", self.self_mxid)

    def mention(self, mxid, body, room, reply_to=None):
        extra = ("--reply-to", reply_to) if reply_to else ()
        return self._call("mention", mxid, body, room, "--agent", self.self_mxid, *extra)

    def read(self, room, limit):
        return self._call("read", room, "--limit", str(limit), "--agent", self.self_mxid)


def _workspace() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def _emit(obj) -> int:
    print(json.dumps(obj, indent=2, default=sorted))
    return 0


def main(argv=None, transport=None, workspace: Optional[Path] = None) -> int:
    p = argparse.ArgumentParser(prog="consult.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("roster", "ask", "match", "pending"):
        s = sub.add_parser(name)
        s.add_argument("--agent", dest="self_mxid", default=os.environ.get("AGENT_MXID"),
                       help="this agent's own mxid (room-ops convention)")
        s.add_argument("--room", default=None, help=f"overrides {policy.CONFIG_ROOM}")
        s.add_argument("--room-cli", default=None, help=f"overrides {policy.CONFIG_ROOM_CLI}")
        if name == "ask":
            s.add_argument("--agent-to", required=True, help="an mxid from `roster`")
            s.add_argument("--question-file", required=True)
            s.add_argument("--task-id", required=True,
                           help="id of the owner task being answered, live in this workspace's inbox")
        if name == "match":
            s.add_argument("--task-id", required=True,
                           help="id of the task the consulted agent's reply arrived as")
        if name == "pending":
            s.add_argument("--nudged", default=None, metavar="CID",
                           help="record that the owner was told this consult is unanswered")
            s.add_argument("--nudge-after", default=None, help=f"overrides {policy.CONFIG_NUDGE_AFTER}")
    a = p.parse_args(argv)

    conf = policy.settings(room=a.room, nudge_after=getattr(a, "nudge_after", None), room_cli=a.room_cli)
    if not conf["active"]:
        return _emit({"ok": False, "inert": True, "reason": conf["reason"]})
    ws = workspace or _workspace()
    if a.cmd == "pending":
        if a.nudged:
            return _emit(policy.mark_nudged(ws, a.nudged))
        return _emit({"ok": True, "nudge_after_s": conf["nudge_after_s"],
                      "pending": policy.pending(ws, conf["nudge_after_s"])})
    try:
        transport = transport or RoomCliTransport(conf["room_cli"], a.self_mxid)
    except RuntimeError as e:
        return _emit({"ok": False, "reason": str(e)})

    if a.cmd == "roster":
        verdict = policy.guard(conf["room"], a.self_mxid or "", transport)
        if not verdict["ok"]:
            return _emit({"ok": False, "reason": verdict["reason"]})
        return _emit({"ok": True, "agents": policy.roster(verdict, a.self_mxid)})

    if a.cmd == "match":
        return _emit({"ok": True, **policy.match_reply(transport, room=conf["room"],
                                                         self_mxid=a.self_mxid or "",
                                                         task_id=a.task_id, workspace=ws)})
    try:
        question = Path(a.question_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _emit({"ok": False, "asked": False, "reason": f"unreadable question: {e}"})
    res = policy.consult(transport, room=conf["room"], self_mxid=a.self_mxid or "",
                         agent=a.agent_to, question=question, task_id=a.task_id, workspace=ws)
    return _emit({"ok": True, **res})


if __name__ == "__main__":
    raise SystemExit(main())
