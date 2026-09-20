#!/usr/bin/env python3
"""CLI over the room-doc client: read, append, replace, peers.

Every subcommand opens the document, does one thing and closes. A long-lived
collaborating agent should import `room_doc_client` instead and hold the
connection open, so its presence stays visible between edits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from room_doc_protocol import RoomDocError  # noqa: E402

TOKEN_VARS = ("AG2_MATRIX_TOKEN", "ROOM_DOC_TOKEN", "MATRIX_ACCESS_TOKEN")
URL_VARS = ("AG2_ROOM_DOC_URL", "AG2_API_ROOT")


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    for var in TOKEN_VARS:
        if os.environ.get(var):
            return os.environ[var]
    raise RoomDocError(
        "no Matrix access token. Pass --token, or set one of: " + ", ".join(TOKEN_VARS) + ".\n"
        "It must be a MATRIX access token — a gateway/relay token is refused (403) by design."
    )


def resolve_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    for var in URL_VARS:
        if os.environ.get(var):
            return os.environ[var]
    raise RoomDocError("no service URL. Pass --url, or set one of: " + ", ".join(URL_VARS) + ".")


def parse_elements(raw: str) -> list:
    """The `draw` argument, turned into elements or a readable refusal.

    Left to json itself this raises JSONDecodeError and the CLI prints a
    traceback, which no other error path here does.
    """
    try:
        elements = json.loads(raw)
    except ValueError as exc:
        raise RoomDocError(
            f"elements must be a JSON array, and this did not parse: {exc}\n"
            'e.g. \'[{"id":"r1","type":"rectangle","x":0,"y":0,'
            '"width":100,"height":60,"version":1}]\'') from exc
    if not isinstance(elements, list):
        raise RoomDocError(
            f"elements must be a JSON ARRAY, got {type(elements).__name__}. "
            "One element still goes in a list.")
    return elements


def render(command: str, *, text: str = "", peers: list | None = None,
           as_json: bool = False, before: int | None = None,
           elements: list | None = None, written: int | None = None) -> str:
    """What the CLI prints, decided without a socket in hand.

    Kept pure so the output contract is testable anywhere: a caller parsing
    stdout as JSON must not find out in production that a mode prints prose.
    """
    peers = peers or []
    if elements is not None:
        # A board read never falls back to the text shape: an empty string
        # there is indistinguishable from an empty board.
        if command == "read":
            if as_json:
                return json.dumps({"elements": elements, "count": len(elements),
                                   "peers": peers}, ensure_ascii=False, indent=2)
            if not elements:
                return "(board is empty — 0 elements)"
            return "\n".join(
                f"{e.get('index') or '-':>6}  {e.get('type','?'):<10} {e.get('id','?')}"
                f"  v{e.get('version','?')}" + ("  [deleted]" if e.get("isDeleted") else "")
                for e in elements)
        if command in ("draw", "erase"):
            return json.dumps({"ok": True, "written": written, "count": len(elements)})
    if command == "read":
        if as_json:
            return json.dumps({"chars": len(text), "peers": peers, "text": text},
                              ensure_ascii=False, indent=2)
        return text
    if command == "peers":
        return json.dumps(peers, ensure_ascii=False, indent=2)
    if command == "append":
        return json.dumps({"ok": True, "before": before, "after": len(text)})
    if command == "replace":
        return json.dumps({"ok": True, "chars": len(text)})
    raise RoomDocError(f"no output defined for {command!r}")


async def run(args: argparse.Namespace) -> int:
    # Imported here, not at module scope: the rules above are pure, and a test
    # of them must not need pycrdt installed.
    from room_doc_client import open_room_doc

    from room_doc_board import BOARD_KIND

    token, url = resolve_token(args.token), resolve_url(args.url)
    async with open_room_doc(url, args.room, token, kind=args.kind,
                             insecure=args.insecure) as doc:
        if args.name:
            await doc.set_presence(args.name, user_id=args.user_id)

        if args.kind == BOARD_KIND:
            # Presence is its own channel and belongs to no document kind, so
            # `peers` is answered here exactly as it is for a text document.
            if args.command == "peers":
                print(render("peers", peers=doc.peers, as_json=args.json))
                return 0
            written = None
            if args.command == "draw":
                written = await doc.put_elements(parse_elements(args.elements))
                await doc.settle(args.settle)
            elif args.command == "erase":
                await doc.delete_element(args.element_id)
                await doc.settle(args.settle)
                written = 1
            elif args.command != "read":
                raise RoomDocError(
                    f"{args.command!r} is a text command; the board holds elements. "
                    "Use read, draw, erase or peers.")
            print(render(args.command, peers=doc.peers, as_json=args.json,
                         elements=doc.elements, written=written))
            return 0

        if args.command in ("draw", "erase"):
            raise RoomDocError(
                f"{args.command!r} needs the board: pass --kind {BOARD_KIND}.")
        before = len(doc.text)
        if args.command == "append":
            await doc.append(args.text)
            await doc.settle(args.settle)
        elif args.command == "replace":
            await doc.replace(args.old, args.new)
            await doc.settle(args.settle)
        print(render(args.command, text=doc.text, peers=doc.peers,
                     as_json=args.json, before=before))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="room_doc", description=__doc__)
    p.add_argument("--url", help="API root or ws(s) URL (else $AG2_ROOM_DOC_URL / $AG2_API_ROOT)")
    p.add_argument("--token", help="Matrix access token (else $AG2_MATRIX_TOKEN)")
    p.add_argument("--name", help="presence name to publish while connected")
    p.add_argument("--user-id", dest="user_id", default=None,
                   help="this agent's mxid, so the roster can show its avatar")
    p.add_argument("--kind", default="markdown",
                   help="which of the room's documents (e.g. board); default markdown")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (local rig only)")
    p.add_argument("--settle", type=float, default=1.0, help="seconds to wait after a write")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    for name, help_text in (("read", "print the document"), ("peers", "who is present")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("room", help="Matrix room id, e.g. !abc:server")

    s = sub.add_parser("append", help="append text to the end")
    s.add_argument("room")
    s.add_argument("text")

    s = sub.add_parser("replace", help="replace the first occurrence of some text")
    s.add_argument("room")
    s.add_argument("old")
    s.add_argument("new")

    s = sub.add_parser("draw", help="write elements to the board (needs --kind board)")
    s.add_argument("room")
    s.add_argument("elements", help="JSON array of Excalidraw-shaped elements")

    s = sub.add_parser("erase", help="mark a board element deleted (needs --kind board)")
    s.add_argument("room")
    s.add_argument("element_id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except RoomDocError as exc:
        print(f"room-doc: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
