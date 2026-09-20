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
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from room_doc_protocol import RoomDocError  # noqa: E402

TOKEN_VARS = ("AG2_MATRIX_TOKEN", "ROOM_DOC_TOKEN", "MATRIX_ACCESS_TOKEN",
              "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_VARS = ("AG2_ROOM_DOC_URL", "AG2_API_ROOT", "REMOTE_TASK_URL")


def split_compound(value: str) -> tuple[str | None, str]:
    """`https://host/relay|secret` -> (origin, secret); a bare token -> (None, token).

    The relay token ships in both shapes, under the same variable names, on
    different installs. Passed whole, the compound form becomes the websocket
    subprotocol header and is refused as invalid before any auth happens.
    """
    head, sep, tail = value.partition("|")
    if sep and tail and re.match(r"^https?://", head):
        return _origin(head), tail
    return None, value


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url.rstrip("/")


def resolve_token(explicit: str | None) -> str:
    raw = explicit or next((os.environ[v] for v in TOKEN_VARS if os.environ.get(v)), None)
    if not raw:
        raise RoomDocError(
            "no access token. Pass --token, or set one of: " + ", ".join(TOKEN_VARS) + ".\n"
            "The agent's ordinary relay token works; a 'url|secret' value is split here."
        )
    return split_compound(raw)[1]


def resolve_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    for var in URL_VARS:
        if os.environ.get(var):
            # The relay URL carries its own path; documents are at its origin.
            return _origin(os.environ[var]) if var == "REMOTE_TASK_URL" else os.environ[var]
    # A compound token names the relay it was minted for; documents live there.
    for var in TOKEN_VARS:
        origin, _ = split_compound(os.environ.get(var, ""))
        if origin:
            return origin
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
           elements: list | None = None, written: int | None = None,
           authors: dict | None = None) -> str:
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
            payload = {"chars": len(text), "peers": peers, "text": text}
            if authors is not None:
                payload["authors"] = authors
            return json.dumps(payload, ensure_ascii=False, indent=2)
        if authors:
            # Named above the text, because an agent decides whether to trust
            # or edit the content by WHO produced it.
            lines = [f"{c}: {a.get('mxid','?')} ({a.get('kind','?')}"
                     + (f", agent of {a['owner_mxid']}" if a.get("owner_mxid") else "") + ")"
                     for c, a in sorted(authors.items())]
            return "authors:\n  " + "\n  ".join(lines) + "\n\n" + text
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

    from room_doc_board import BOARD_KIND, place_clear

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
                elements = parse_elements(args.elements)
                # Unless the coordinates are final, a drawing that would land
                # on someone else's is moved below it.
                if not args.absolute:
                    elements = place_clear(elements, doc.elements)
                written = await doc.put_elements(elements)
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
                         elements=doc.elements, written=written,
                         authors=doc.authors if args.with_authors else None))
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
                     as_json=args.json, before=before,
                     authors=doc.authors if args.with_authors else None))
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
    p.add_argument("--with-authors", dest="with_authors", action="store_true",
                   help="also report who wrote with each Yjs client id")
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
    s.add_argument("--absolute", action="store_true",
                   help="write the coordinates as given, even onto existing drawings")

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
