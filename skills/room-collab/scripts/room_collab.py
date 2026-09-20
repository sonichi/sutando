#!/usr/bin/env python3
"""CLI over the room-collab client: read, append, replace, peers.

Every subcommand opens the document, does one thing and closes. A long-lived
collaborating agent should import `room_collab_client` instead and hold the
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

from room_collab_protocol import DEFAULT_KIND, RoomDocError  # noqa: E402

TOKEN_VARS = ("AG2_MATRIX_TOKEN", "ROOM_DOC_TOKEN", "MATRIX_ACCESS_TOKEN",
              "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_VARS = ("AG2_ROOM_DOC_URL", "AG2_API_ROOT", "REMOTE_TASK_URL")
IDENTITY_VARS = ("AG2SPACE_USER_ID", "AG2_MATRIX_USER_ID")


def resolve_identity(explicit: str | None) -> str:
    """The mxid a kanban write is signed with (`by`). The panel tie-breaks on
    it, so it must be the same string on every write from this agent."""
    who = explicit or next((os.environ[v] for v in IDENTITY_VARS if os.environ.get(v)), None)
    if not who:
        raise RoomDocError("no identity for `by`. Pass --user-id, or set one of: "
                           + ", ".join(IDENTITY_VARS) + ".")
    return who


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


def credential_report(explicit_token: str | None, explicit_url: str | None,
                      environ: dict | None = None) -> list[tuple[str, bool, str]]:
    """Where the token and URL would come from, without printing the secret.

    A new agent's first failure is discovery, not authorization: which variable,
    which shape, which host. Each row names one step so the failing one is
    visible on its own.
    """
    env = os.environ if environ is None else environ
    rows: list[tuple[str, bool, str]] = []
    src = "--token" if explicit_token else next((v for v in TOKEN_VARS if env.get(v)), None)
    if not src:
        rows.append(("token", False, "none found; set one of " + ", ".join(TOKEN_VARS)))
    else:
        raw = explicit_token or env[src]
        origin, secret = split_compound(raw)
        shape = f"compound (url|secret, relay {origin})" if origin else "bare"
        rows.append(("token", True, f"from {src}, {shape}, {len(secret)} chars"))
    url_src = "--url" if explicit_url else next((v for v in URL_VARS if env.get(v)), None)
    if url_src:
        rows.append(("url", True, f"from {url_src}"))
    else:
        compound = next((v for v in TOKEN_VARS if split_compound(env.get(v, ""))[0]), None)
        if compound:
            rows.append(("url", True, f"origin of the compound token in {compound}"))
        else:
            rows.append(("url", False, "none found; set one of " + ", ".join(URL_VARS)))
    return rows


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


async def doctor(args: argparse.Namespace) -> int:
    """Every step a first connection needs, reported one line each and stopped
    at the first failure — so the failing STEP is the answer, not a symptom."""
    from room_collab_client import open_room_collab

    def say(step: str, ok: bool, detail: str) -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {step:<8} {detail}")

    print(f"room-collab doctor: {args.room} (kind {args.kind})")
    try:
        import pycrdt  # noqa: F401
        import websockets  # noqa: F401
        say("deps", True, "pycrdt + websockets importable")
    except ImportError as exc:
        say("deps", False, f"{exc}; pip install -r skills/room-collab/requirements.txt")
        return 2
    rows = credential_report(args.token, args.url)
    for step, ok, detail in rows:
        say(step, ok, detail)
    if not all(ok for _, ok, _ in rows):
        return 2
    token, url = resolve_token(args.token), resolve_url(args.url)
    try:
        async with open_room_collab(url, args.room, token, kind=args.kind,
                                 insecure=args.insecure) as doc:
            say("connect", True, f"{url} accepted the socket")
            if args.kind == DEFAULT_KIND:
                say("read", True, f"{len(doc.text)} chars in the document")
            else:
                say("read", True, f"{len(doc.elements)} elements")
            say("peers", True, f"{len(doc.peers)} present")
    except RoomDocError as exc:
        say("connect", False, str(exc))
        return 2
    print("  all steps passed — connected and read; writes go over this same connection")
    return 0


async def kanban(doc, args: argparse.Namespace) -> int:
    """The board of cards: read it, or write one card through the panel's
    own rules. Every write is a newer version signed with this agent's mxid."""
    import time

    from room_kanban import (assign_card, default_columns, delete_card, in_column, live_cards,
                             move_card, new_card, order_after_last, orphaned_cards)

    if args.command == "peers":
        print(render("peers", peers=doc.peers, as_json=args.json))
        return 0
    if args.command == "read":
        cols, cards = doc.columns, doc.cards
        if args.json:
            print(json.dumps({"columns": cols, "cards": live_cards(cards),
                              "orphaned": orphaned_cards(cards, [(c["id"], c) for c in cols]),
                              "peers": doc.peers}, ensure_ascii=False, indent=2))
            return 0
        if not cols and not live_cards(cards):
            print("(kanban is empty — no columns, no cards)")
            return 0
        for col in cols:
            rows = in_column(cards, col["id"])
            print(f"## {col['title']}  [{col['id']}]  ({len(rows)})")
            for c in rows:
                who = f"  → {c['assignee']}" if c.get("assignee") else ""
                print(f"  {c['id']}  {c['text']}{who}")
        lost = orphaned_cards(cards, [(c["id"], c) for c in cols])
        if lost:
            print(f"## (no column)  ({len(lost)})")
            for c in lost:
                print(f"  {c['id']}  {c['text']}  [column {c['column']!r} does not exist]")
        return 0

    now = int(time.time() * 1000)
    by = resolve_identity(args.user_id)
    stored = {k: v for k, v in doc.cards}
    if args.command == "add":
        if not doc.columns:
            # The panel seeds these on first open; the same ids and order here
            # mean the two sides agree about which column is which.
            await doc.put_columns(default_columns(now, by))
        column = args.column or "todo"
        if column not in {c["id"] for c in doc.columns}:
            raise RoomDocError(f"no column {column!r}; the board has: "
                               + ", ".join(c["id"] for c in doc.columns))
        ident = args.id or f"card-{now:x}"
        card = new_card(ident, column, args.text, now, by,
                        order_after_last(doc.cards, column), args.assign or "")
        written = await doc.put_cards([card])
        await doc.settle(args.settle)
        print(json.dumps({"ok": True, "written": written, "card": card}, ensure_ascii=False))
        return 0
    # The verb before the card, so a text command is refused by name.
    if args.command not in ("move", "assign", "erase"):
        raise RoomDocError(f"{args.command!r} is not a kanban command; use read, add, move, "
                           "assign, erase, watch or peers.")
    cid = getattr(args, "card_id", None) or getattr(args, "element_id", None)
    card = stored.get(cid)
    if card is None or card.get("deleted"):
        raise RoomDocError(f"no live card {cid!r}")
    if args.command == "move":
        if args.column not in {c["id"] for c in doc.columns}:
            raise RoomDocError(f"no column {args.column!r}")
        nxt = move_card(card, args.column, order_after_last(doc.cards, args.column), now, by)
    elif args.command == "assign":
        nxt = assign_card(card, args.assignee, now, by)
    else:
        nxt = delete_card(card, now, by)
    written = await doc.put_cards([nxt])
    await doc.settle(args.settle)
    print(json.dumps({"ok": True, "written": written, "card": nxt}, ensure_ascii=False))
    return 0



async def watch(args: argparse.Namespace, token: str, url: str) -> int:
    """Hold the document open and print one line per event that concerns
    `--for`, as it lands. Comes back from a service restart with the last
    snapshot in hand, so what landed meanwhile is reported, not skipped.
    Exits (rc 2) only on a refusal or after --max-reconnects failures."""
    from room_collab_client import open_room_collab
    from room_collab_protocol import RECONNECT_CODES

    handles = args.handles or []
    since = None
    failures = 0
    print(f"watching {args.room} ({args.kind}) for {handles or 'nobody in particular'}; "
          f"reporting after {args.settle}s of quiet", flush=True)
    while True:
        try:
            async with open_room_collab(url, args.room, token, kind=args.kind,
                                     insecure=args.insecure) as doc:
                if args.name:
                    await doc.set_presence(args.name, user_id=args.user_id)
                if since is not None:
                    print("RECONNECTED\tcatching up on what landed meanwhile", flush=True)
                async for ev in doc.events(handles, settle=args.settle, since=since):
                    failures = 0              # a socket that carries an event is a real one
                    kind = ev.pop("kind")
                    detail = ev.pop("text", None)
                    rest = " ".join(f"{k}={v}" for k, v in ev.items() if v not in (None, ""))
                    print(f"EVENT\t{kind}\t{rest}" + (f"\t{detail}" if detail else ""), flush=True)
                return 0                      # a clean end is an end, not a reconnect
        except RoomDocError as exc:
            since = getattr(exc, "snapshot", since)
            if exc.code not in RECONNECT_CODES or failures >= args.max_reconnects:
                raise
            failures += 1
            wait = min(2 ** failures, 30)
            print(f"RECONNECTING\tcode={exc.code} attempt={failures} in {wait}s", flush=True)
            await asyncio.sleep(wait)


async def run(args: argparse.Namespace) -> int:
    # Imported here, not at module scope: the rules above are pure, and a test
    # of them must not need pycrdt installed.
    from room_collab_client import open_room_collab

    from room_collab_board import BOARD_KIND, place_clear
    from room_kanban import KANBAN_KIND

    if args.command == "doctor":
        return await doctor(args)

    token, url = resolve_token(args.token), resolve_url(args.url)
    if args.command == "watch":
        return await watch(args, token, url)

    async with open_room_collab(url, args.room, token, kind=args.kind,
                             insecure=args.insecure) as doc:
        if args.name:
            await doc.set_presence(args.name, user_id=args.user_id)

        if args.kind == KANBAN_KIND:
            return await kanban(doc, args)

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
    p = argparse.ArgumentParser(prog="room_collab", description=__doc__)
    p.add_argument("--url", help="service origin (else $AG2_ROOM_DOC_URL, $AG2_API_ROOT, or the relay's)")
    p.add_argument("--token", help="bearer; the relay token works (else the env, see SKILL.md)")
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

    for name, help_text in (("read", "print the document"), ("peers", "who is present"),
                            ("doctor", "check deps, credential, URL and connection, step by step")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("room", help="Matrix room id, e.g. !abc:server")

    s = sub.add_parser("watch", help="hold the document open; print each event that concerns --for")
    s.add_argument("room")
    s.add_argument("--for", dest="handles", action="append", metavar="HANDLE",
                   help="a name or @mxid to watch for (repeatable)")
    s.add_argument("--max-reconnects", type=int, default=20,
                   help="give up after this many consecutive failed reconnects")

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

    s = sub.add_parser("erase", help="mark a board element or kanban card deleted")
    s.add_argument("room")
    s.add_argument("element_id")

    s = sub.add_parser("add", help="add a kanban card (needs --kind kanban)")
    s.add_argument("room")
    s.add_argument("text")
    s.add_argument("--column", help="column id; default todo")
    s.add_argument("--assign", metavar="MXID", help="who it is for")
    s.add_argument("--id", help="card id; default generated")

    s = sub.add_parser("move", help="move a kanban card to a column (needs --kind kanban)")
    s.add_argument("room")
    s.add_argument("card_id")
    s.add_argument("column")

    s = sub.add_parser("assign", help="assign a kanban card (needs --kind kanban)")
    s.add_argument("room")
    s.add_argument("card_id")
    s.add_argument("assignee", help="an mxid, or '' for nobody")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except RoomDocError as exc:
        print(f"room-collab: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
