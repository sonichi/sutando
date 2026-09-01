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
    python3 room_ops.py rooms  [--agent mxid]                        # joined-rooms list
    python3 room_ops.py members <room_id> [--agent mxid]             # room member list
    python3 room_ops.py events emit <room> --type space.ag2.app.x --content '{"k":1}'
    python3 room_ops.py events subscribe <room> --types a,b [--filters json]
    python3 room_ops.py events unsubscribe <room>
    python3 room_ops.py events list
    python3 room_ops.py events pull [--cursor N] [--wait S]
    python3 room_ops.py events stream [--cursor-file PATH] [--once] [--max-events N]

Every subcommand prints a structured JSON result and **exits 0** for any
structured result (a graceful `ok:false` "no context / no-op" is not a failed
task); usage errors exit 2. See SKILL.md for the boundary + the parity epic.
`events stream` is the one JSONL surface: one compact JSON line per delivered
event (journal-friendly), then a one-line summary.
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
import resolve as _resolve # noqa: E402
import mention as _mention # noqa: E402
import say as _say         # noqa: E402
import rooms as _rooms     # noqa: E402
import members as _members # noqa: E402
import events as _events   # noqa: E402


def _events_stream(a):
    """`events stream`: one compact JSON line per event (journal-friendly), a
    one-line JSON summary last. Exits 0 for any structured outcome — a
    disconnect without --cursor-file is a structured ok:false, not a crash.
    With --cursor-file the durable-cursor wrapper reconnects forever (#184);
    without it, one connection is made and its end is reported."""
    max_events = 1 if a.once else a.max_events
    seen = {"n": 0, "cursor": None}

    def on_event(cur, envelope):
        seen["n"] += 1
        seen["cursor"] = cur
        print(json.dumps(envelope, ensure_ascii=False), flush=True)

    try:
        if a.cursor_file:
            cur = _events.stream_with_resume(a.cursor_file, on_event, max_events=max_events)
        else:
            cur = _events.stream(cursor=a.cursor, on_event=on_event, max_events=max_events)
        out = {"ok": True, "events": seen["n"], "cursor": cur}
    except KeyboardInterrupt:
        out = {"ok": True, "events": seen["n"], "cursor": seen["cursor"], "reason": "interrupted"}
    except (_events.StreamDisconnected, RuntimeError) as e:
        out = {"ok": False, "events": seen["n"], "cursor": seen["cursor"], "reason": str(e)}
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


def _dispatch_events(a):
    if a.events_cmd == "emit":
        try:
            content = json.loads(a.content)
        except ValueError as e:
            return {"ok": False, "reason": f"--content is not valid JSON: {e}"}
        # The wire contract is a JSON object; a bare scalar or list would be
        # rejected server-side, so name it here rather than spend a round trip.
        if not isinstance(content, dict):
            return {"ok": False, "reason": "--content must be a JSON object"}
        return _events.emit(a.room_id, a.type, content, agent_mxid=a.agent_mxid)
    if a.events_cmd == "subscribe":
        types = [t.strip() for t in (a.types or "").split(",") if t.strip()]
        filters = None
        if a.filters:
            try:
                filters = json.loads(a.filters)
            except ValueError as e:
                # Structured error, exit 0 — same convention as doc's --file failure.
                return {"ok": False, "reason": f"--filters is not valid JSON: {e}"}
        return _events.subscribe(a.room_id, types, filters=filters, agent_mxid=a.agent_mxid)
    if a.events_cmd == "unsubscribe":
        return _events.unsubscribe(a.room_id, agent_mxid=a.agent_mxid)
    if a.events_cmd == "list":
        return _events.subscriptions(a.agent_mxid)
    return _events.pull(cursor=a.cursor, wait=a.wait)  # pull


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

    p = sub.add_parser("doc", help="read/write/delete a room Context document")
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

    p = sub.add_parser("rooms", help="list this agent's joined rooms")
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    p = sub.add_parser("members", help="list a room's members (user_id, display name, kind)")
    p.add_argument("room_id")
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))

    p = sub.add_parser("events", help="event subscriptions + delivery (#184 client half)")
    esub = p.add_subparsers(dest="events_cmd", required=True)
    e = esub.add_parser("emit", help="send one typed space.ag2.* timeline event as this agent")
    e.add_argument("room_id")
    e.add_argument("--type", required=True, help="event type (e.g. space.ag2.app.card)")
    e.add_argument("--content", required=True, help="JSON object body")
    e.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    e = esub.add_parser("subscribe", help="subscribe this agent to a room's events")
    e.add_argument("room_id")
    e.add_argument("--types", required=True,
                   help="comma-separated event types (e.g. message.created,reaction.added)")
    e.add_argument("--filters", default=None, help="JSON filter object (passed through verbatim)")
    e.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    e = esub.add_parser("unsubscribe", help="drop this agent's subscription on a room")
    e.add_argument("room_id")
    e.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    e = esub.add_parser("list", help="list this agent's own subscriptions")
    e.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    e = esub.add_parser("pull", help="one long-poll round for pending events")
    e.add_argument("--cursor", type=int, default=0)
    e.add_argument("--wait", type=int, default=_events.DEFAULT_WAIT)
    e = esub.add_parser("stream", help="SSE-follow events; one JSON line per event")
    e.add_argument("--cursor-file", default=None,
                   help="durable resume cursor (enables reconnect-with-backoff)")
    e.add_argument("--cursor", type=int, default=None,
                   help="explicit start cursor (no --cursor-file)")
    e.add_argument("--once", action="store_true", help="exit after the first event")
    e.add_argument("--max-events", type=int, default=None)

    p = sub.add_parser("resolve", help="resolve a friendly handle -> agent mxid (via /v1/agents)")
    p.add_argument("handle")

    p = sub.add_parser("mention", help="@-mention an agent by handle (resolve + post a triggering message)")
    p.add_argument("handle")
    p.add_argument("message")
    p.add_argument("room_id")
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    p.add_argument("--reply-to", dest="reply_to", default=None,
                   help="event id ($abc) to cite as the message replied to. This is a "
                        "CITATION: the post stays in the main timeline. It does NOT put "
                        "the post in a Matrix thread — the gateway has no field for that.")

    p = sub.add_parser("say", help="post a plain message into a room (mentions no one)")
    p.add_argument("room_id")
    p.add_argument("message")
    p.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    p.add_argument("--reply-to", dest="reply_to", default=None,
                   help="event id ($abc) to cite as the message replied to. This is a "
                        "CITATION: the post stays in the main timeline. It does NOT put "
                        "the post in a Matrix thread — the gateway has no field for that.")

    p = sub.add_parser("grant", help="make a room authoritative — its access policy "
                                     "GRANTS access, overriding agents' local allowFrom (#429)")
    p.add_argument("room_id")
    p.add_argument("--tier", dest="tiers", action="append", metavar="@user:hs=owner|guest",
                   help="grant a specific member a tier (repeatable)")
    p.add_argument("--default-tier", dest="default_tier", choices=("owner", "guest"),
                   help="tier for members not named by --tier")
    p.add_argument("--revoke", action="store_true",
                   help="disable the grant (authoritative=false); leaves other policy fields intact")
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
            try:
                content = open(a.file).read() if a.file else _sys.stdin.read()
            except (OSError, UnicodeDecodeError) as e:
                content = None
                res = {"ok": False, "reason": f"cannot read --file {a.file}: {e}"}
            if content is not None:
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
    elif a.cmd == "rooms":
        res = _rooms.joined_rooms(a.agent_mxid)
    elif a.cmd == "members":
        res = _members.room_members(a.room_id, a.agent_mxid)
    elif a.cmd == "events":
        if a.events_cmd == "stream":
            return _events_stream(a)  # prints JSONL itself; summary is one line
        res = _dispatch_events(a)
    elif a.cmd == "resolve":
        res = _resolve.resolve_user(a.handle)
    elif a.cmd == "mention":
        res = _mention.mention(a.handle, a.message, a.room_id, a.agent_mxid,
                               reply_to=a.reply_to)
    elif a.cmd == "say":
        res = _say.say(a.message, a.room_id, a.agent_mxid,
                       reply_to=a.reply_to)
    elif a.cmd == "grant":
        import grant as _grant
        try:
            tiers = _grant.parse_tier_pairs(a.tiers)
        except ValueError as e:
            res = {"ok": False, "reason": str(e)}
        else:
            res = _grant.grant_room(a.room_id, tiers=tiers, default_tier=a.default_tier,
                                    revoke=a.revoke, agent_mxid=a.agent_mxid)
    else:  # react / unreact
        key = a.key or _react.ACK[a.ack]
        fn = _react.react if a.cmd == "react" else _react.unreact
        res = fn(a.room_id, a.event_id, key, a.agent_mxid)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))