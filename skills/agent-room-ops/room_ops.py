#!/usr/bin/env python3
"""room-ops — an agent's room-participation capability collection (one skill).

A single gateway-only client surface for everything an agent does in a room beyond
the task inbox: read history, send/fetch native media, react to events, … Each
capability is a module sharing `_gateway.py` (gateway coords + the per-agent gate +
graceful-degrade); this file is the unified CLI that dispatches to them.

    python3 room_ops.py read   <room> [--limit N] [--before tok] [--agent mxid]
    python3 room_ops.py fetch  <ref>  [--room r] [--agent mxid]      # media in
    python3 room_ops.py send   <room> <path> [--caption c] [--agent mxid]  # media out
    python3 room_ops.py react  <room> <event_id> (--ack received|working|done|fail | --key 🎉) [--agent mxid]
    python3 room_ops.py unreact <room> <event_id> (--ack … | --key …) [--agent mxid]
    python3 room_ops.py join   <room> [--agent mxid]                 # accept own invite

Every subcommand prints a structured JSON result and **exits 0** for any
structured result (a graceful `ok:false` "no context / no-op" is not a failed
task); usage errors exit 2. See SKILL.md for the boundary + the parity epic.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import read as _read       # noqa: E402
import media as _media     # noqa: E402
import react as _react     # noqa: E402
import join as _join       # noqa: E402


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="room_ops", description="Agent room-participation ops (gateway-only, gated).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read", help="pull recent room history")
    p.add_argument("room_id")
    p.add_argument("--limit", type=int, default=_read.DEFAULT_LIMIT)
    p.add_argument("--before", default=None)
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    p = sub.add_parser("fetch", help="fetch a shared media ref -> local path")
    p.add_argument("ref")
    p.add_argument("--room", dest="room_id", default=None)
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    p = sub.add_parser("send", help="upload a local file into a room")
    p.add_argument("room_id")
    p.add_argument("path")
    p.add_argument("--caption", default=None)
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    p = sub.add_parser("doc", help="read/write/delete a room vault document")
    p.add_argument("action", choices=["get", "put", "rm"])
    p.add_argument("room")
    p.add_argument("--folder", default="room-live-context")
    p.add_argument("--name", help="document filename (e.g. TODO.md)")
    p.add_argument("--file", help="put: local file to upload (else stdin)")
    p.add_argument("--message", help="put: commit message")
    p.add_argument("--agent")

    p = sub.add_parser("join", help="accept this agent's own pending room invite")
    p.add_argument("room_id")
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    for name in ("react", "unreact"):
        p = sub.add_parser(name, help=f"{name} on a room event")
        p.add_argument("room_id")
        p.add_argument("event_id")
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--key")
        g.add_argument("--ack", choices=sorted(_react.ACK))
        p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    a = ap.parse_args(argv)
    if a.cmd == "read":
        res = _read.read_room(a.room_id, a.agent_mxid, a.limit, before=a.before)
    elif a.cmd == "fetch":
        res = _media.fetch_media(a.ref, a.agent_mxid, a.room_id)
    elif a.cmd == "send":
        res = _media.send_media(a.room_id, a.path, a.agent_mxid, caption=a.caption)
    elif a.cmd == "doc":
        import doc as _doc
        if a.action == "get":
            res = _doc.doc_get(a.room, folder=a.folder, name=a.name, agent_mxid=a.agent)
        elif a.action == "put":
            import sys as _sys
            content = open(a.file).read() if a.file else _sys.stdin.read()
            res = _doc.doc_put(a.room, content, folder=a.folder,
                               name=a.name or "CONTEXT.md", message=a.message,
                               agent_mxid=a.agent)
        else:
            if not a.name:
                res = {"ok": False, "reason": "--name is required for rm"}
            else:
                res = _doc.doc_rm(a.room, a.name, folder=a.folder, agent_mxid=a.agent)
    elif a.cmd == "join":
        res = _join.join_room(a.room_id, a.agent_mxid)
    else:  # react / unreact
        key = a.key or _react.ACK[a.ack]
        fn = _react.react if a.cmd == "react" else _react.unreact
        res = fn(a.room_id, a.event_id, key, a.agent_mxid)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
