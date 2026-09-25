#!/usr/bin/env python3
"""CLI over the room-collab client: read, append, replace, comment, peers.

Every subcommand opens the document, does one thing and closes. A long-lived
collaborating agent should import `room_collab_client` instead and hold the
connection open, so its presence stays visible between edits.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from room_collab_protocol import DEFAULT_KIND, HTML_KIND, TEXT_ROOTS, RoomDocError  # noqa: E402
from room_collab_watch import new_lines  # noqa: E402

# The edge refuses urllib's default agent outright (Cloudflare 1010), so an
# HTTP read must say who it is. The websocket path sends its own.
USER_AGENT = "room-collab-skill/1 (+https://ag2.space)"
# The web client's collabKey('doc', 'comment'): a room message carrying it is a comment.
COMMENT_KEY = "space.ag2.collab.doc.comment"
# collabKey('doc', 'summon'): an agent's summon must be the SAME event the
# client's @-picker writes, or its timeline has no card to render.
SUMMON_KEY = "space.ag2.collab.doc.summon"
SUMMON_CONTEXT_MAX = 400
# An mxid with a server part. The client's own reader refuses anything else, so
# a summon naming "qingyun" would post a message that renders as plain prose.
MXID_RE = re.compile(r"^@[^\s:]+:\S+$")
# The surface as the summon's prose names it; the marker carries `kind` verbatim.
SUMMON_SURFACE = {"markdown": "Doc", "board": "whiteboard", "kanban": "kanban board",
                  "html": "HTML page", "sheet": "sheet"}
# The client refuses a longer selection rather than truncating the quote it verifies by.
QUOTE_MAX = 2000

# The collab names lead; the ROOM_DOC_* spellings are read for one release
# more so an install that set them keeps working through the rename.
TOKEN_VARS = ("AG2_MATRIX_TOKEN", "ROOM_COLLAB_TOKEN", "ROOM_DOC_TOKEN", "MATRIX_ACCESS_TOKEN",
              "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_VARS = ("AG2_ROOM_COLLAB_URL", "AG2_ROOM_DOC_URL", "AG2_API_ROOT", "REMOTE_TASK_URL")
IDENTITY_VARS = ("AG2SPACE_USER_ID", "AG2_MATRIX_USER_ID")


def resolve_identity(explicit: str | None) -> str:
    """The mxid a kanban or sheet write is signed with (`by`). The panel tie-breaks on
    it, so it must be the same string on every write from this agent."""
    who = explicit or next((os.environ[v] for v in IDENTITY_VARS if os.environ.get(v)), None)
    if not who:
        raise RoomDocError("no identity for `by`. Pass --user-id, or set one of: "
                           + ", ".join(IDENTITY_VARS) + ".")
    return who


def own_handles(args: argparse.Namespace) -> list[str]:
    """The names a watcher answers to when none were given: its mxid, the
    localpart of it, and its presence name. A summon writes the room's display
    name for the agent, which only the summon message shows — pass that with
    --for; these defaults cover the forms the agent knows about itself."""
    who = args.user_id or next((os.environ[v] for v in IDENTITY_VARS if os.environ.get(v)), "")
    out = []
    if who:
        out.append(who)
        local = who.lstrip("@").split(":", 1)[0]
        if local and local != who:
            out.append(local)
    if getattr(args, "name", None) and args.name not in out:
        out.append(args.name)
    return out


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


def delta_since(previous: str | None, current: str) -> list[str]:
    """The lines that are new since `previous`; everything when there is none.

    A summoned agent that comes back should read what changed before it reads
    everything again. Same rule as `watch` uses for a remote edit."""
    if previous is None:
        return [line for line in current.split("\n") if line.strip()]
    return new_lines(previous, current)


def snapshot_path(workspace: Path, room: str, kind: str, who: str = "") -> Path:
    """Where ONE reader keeps what it last read of one surface: per room, kind and
    reader, hashed so a room id's `!` and `:` never touch the filesystem. Several
    seats share a workspace, so a key without the reader would report "new since
    someone else read"."""
    key = hashlib.sha1(f"{room}\n{kind}\n{who}".encode("utf-8")).hexdigest()[:16]
    return Path(workspace) / "state" / "room-collab" / f"last-read-{key}.txt"


def reader_identity(args: argparse.Namespace) -> str:
    """Who is reading: the mxid when known, else the presence name, else nobody."""
    who = getattr(args, "user_id", None) or next(
        (os.environ[v] for v in IDENTITY_VARS if os.environ.get(v)), None)
    return who or getattr(args, "name", None) or ""


def recall(path: Path) -> tuple[str | None, float | None]:
    try:
        return path.read_text(encoding="utf-8"), path.stat().st_mtime
    except OSError:
        return None, None


def remember(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _workspace(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    sys.path.insert(0, str(HERE.parent.parent.parent / "src"))
    from workspace_default import resolve_workspace  # noqa: WPS433
    return Path(resolve_workspace())


def _refusal(exc) -> str:
    """Why a presence read was refused, naming the EDGE when the edge did it.

    Cloudflare answers `error code: 1010` to a request whose user agent it does
    not like, and core-api never sees it — so blaming membership or the token
    sends the reader to check two things that are both fine.
    """
    try:
        body = exc.read().decode("utf-8", "replace")[:200]
    except Exception:  # noqa: BLE001 - an unreadable body must not mask the status
        body = ""
    if "1010" in body:
        return ("refused by the edge in front of the service (Cloudflare 1010), not by the "
                "service — the request never reached it. Its user agent was rejected.")
    if exc.code == 403:
        return "not a member, or the token was rejected"
    return "the service did not answer it"


def presence_summary(url: str, room: str, token: str, opener=None) -> dict:
    """Who is in each of the room's surfaces, from the service — without opening
    any of them. The same answer the header's live dot is drawn from."""
    origin = url.rstrip("/")
    if "/api/v1/room-collab" not in origin and "/api/v1/room-doc" not in origin:
        origin = f"{origin}/api/v1/room-collab"
    endpoint = f"{origin}/{urllib.parse.quote(room, safe='')}/presence"
    req = urllib.request.Request(endpoint, headers={"Authorization": f"Bearer {token}",
                                                    "User-Agent": USER_AGENT})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RoomDocError(f"presence refused ({exc.code}) at {endpoint}: "
                           + _refusal(exc)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RoomDocError(f"presence unreachable at {endpoint}: {exc}") from exc
    surfaces = body.get("surfaces") if isinstance(body, dict) else None
    if not isinstance(surfaces, dict):
        raise RoomDocError(f"presence answered without surfaces at {endpoint}")
    def count(v) -> int:
        return v if isinstance(v, int) and v >= 0 else 0   # junk reads as nobody

    out = {}
    for kind, counts in surfaces.items():
        if isinstance(counts, dict):
            out[str(kind)] = {"peers": count(counts.get("peers")),
                              "agents": count(counts.get("agents"))}
    return out


def render_presence(room: str, surfaces: dict, as_json: bool) -> str:
    if as_json:
        return json.dumps({"room": room, "surfaces": surfaces}, ensure_ascii=False, indent=2)
    if not any(v["peers"] for v in surfaces.values()):
        return f"nobody is in any surface of {room}"
    rows = []
    for kind, v in sorted(surfaces.items(), key=lambda kv: (-kv[1]["peers"], kv[0])):
        people = v["peers"] - v["agents"]
        who = ", ".join(p for p in (f"{v['agents']} agent(s)" if v["agents"] else "",
                                    f"{people} person(s)" if people else "") if p)
        rows.append(f"  {kind:<12} {v['peers']} present" + (f"  ({who})" if who else ""))
    return "\n".join(rows)


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
    # Imported after the deps check: this module exits at import when the deps
    # are absent, which would pre-empt the step the check exists to report.
    from room_collab_client import open_room_collab
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
            if args.kind in TEXT_ROOTS:
                say("read", True, f"{len(doc.text)} chars in the document")
            else:
                say("read", True, f"{len(doc.elements)} elements")
            say("peers", True, f"{len(doc.peers)} present")
    except RoomDocError as exc:
        say("connect", False, str(exc))
        return 2
    print("  all steps passed — connected and read; writes go over this same connection")
    return 0


def fetch_library(base: str, name: str) -> bytes:
    """One file of the template library the web client serves; the same files it lists."""
    if not re.fullmatch(r"index\.json|[a-z0-9-]+\.html", name):
        raise RoomDocError(f"not a library file: {name!r}")
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/" + name, timeout=15) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RoomDocError(f"could not read the template library at {base}: {exc}") from exc


async def templates(doc, args: argparse.Namespace, url: str) -> int:
    """List the library, or start the page from one template (refusing to
    overwrite someone's page unless --replace says to)."""
    if args.kind != HTML_KIND:
        raise RoomDocError(f"templates are for the HTML page: pass --kind {HTML_KIND}.")
    base = args.library or _origin(url) + "/html-templates/"
    index = json.loads(fetch_library(base, "index.json"))
    items = [t for t in index.get("templates", []) if isinstance(t, dict)]
    if not args.use:
        if args.json:
            print(json.dumps(index, ensure_ascii=False))
            return 0
        for t in items:
            print(f"{t.get('id')}: {t.get('name')} — {t.get('description', '')}")
        scope = index.get("scope") or {}
        for s in scope.get("supported", []):
            print(f"  supported: {s}")
        for s in scope.get("not_supported", []):
            print(f"  not supported: {s}")
        return 0
    chosen = next((t for t in items if t.get("id") == args.use), None)
    if not chosen:
        raise RoomDocError(f"no template {args.use!r}; run `templates` to list them.")
    if doc.text.strip() and not args.replace:
        raise RoomDocError("the page already has content; pass --replace to overwrite it "
                           "for everyone in the room.")
    body = fetch_library(base, str(chosen.get("file", ""))).decode("utf-8")
    if doc.text:
        await doc.replace(doc.text, body)
    else:
        await doc.append(body)
    await doc.settle(args.settle)
    print(json.dumps({"ok": True, "template": args.use, "chars": len(body)}))
    return 0


async def sheet(doc, args: argparse.Namespace) -> int:
    """The room's sheet: read its inputs, set one cell, or import a CSV block."""
    from room_sheet import grid, plan_writes, read_csv, starter_axes, to_csv

    rows, cols, cells = doc.sheet
    if not rows or not cols:
        # A room that has never had a sheet gets the same blank grid the web client makes.
        start_rows, start_cols = starter_axes()
        await doc.put_sheet(start_rows if not rows else {}, start_cols if not cols else {}, {})
        rows, cols, cells = doc.sheet
    if args.command == "peers":
        print(render("peers", peers=doc.peers, as_json=args.json))
        return 0
    if args.command == "read":
        block = grid(rows, cols, cells)
        if args.json:
            from room_sheet import col_name
            print(json.dumps({f"{col_name(c)}{r + 1}": v for r, line in enumerate(block)
                              for c, v in enumerate(line) if v}, ensure_ascii=False, indent=2))
        else:
            print(to_csv(block) or "(the sheet is empty)", end="")
        return 0
    try:
        if args.command == "set":
            new_rows, new_cols, writes = plan_writes(rows, cols, args.cell, [[args.value]],
                                                     resolve_identity(args.user_id))
        elif args.command == "import":
            with open(args.file, encoding="utf-8-sig") as fh:
                block = read_csv(fh.read())
            new_rows, new_cols, writes = plan_writes(rows, cols, args.at, block,
                                                     resolve_identity(args.user_id))
        else:
            raise RoomDocError(f"{args.command!r} is not a sheet command; use read, set, import or peers.")
    except ValueError as exc:
        raise RoomDocError(str(exc)) from None
    n = await doc.put_sheet(new_rows, new_cols, writes)
    await doc.settle(args.settle)
    print(json.dumps({"ok": True, "cells": n, "rows_added": len(new_rows), "cols_added": len(new_cols)}))
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



def presence_name(name: "str | None", user_id: "str | None") -> "str | None":
    """The name to publish presence under: the given one, else the mxid's localpart.

    A peer with no name is not rendered — the web client skips it and the
    service's summary counts it without naming it — so publishing nameless is
    indistinguishable from not joining. An mxid already carries a usable name.
    """
    if name and name.strip():
        return name.strip()
    if isinstance(user_id, str) and user_id.startswith("@") and ":" in user_id:
        local = user_id[1:].split(":", 1)[0].strip()
        return local or None
    return None


async def watch(args: argparse.Namespace, token: str, url: str) -> int:
    """Hold the surface open and print one line per event that concerns
    `--for`, as it lands. Comes back from a service restart with the last
    snapshot in hand, so what landed meanwhile is reported, not skipped.
    Exits (rc 2) only on a refusal or after --max-reconnects failures."""
    from room_collab_client import open_room_collab
    from room_collab_protocol import is_transient

    handles = args.handles or own_handles(args)
    since = None
    failures = 0
    print(f"watching {args.room} ({args.kind}) for {handles or 'nobody in particular'}; "
          f"reporting after {args.settle}s of quiet", flush=True)
    while True:
        try:
            async with open_room_collab(url, args.room, token, kind=args.kind,
                                     insecure=args.insecure) as doc:
                announce = presence_name(args.name, args.user_id)
                if announce:
                    await doc.set_presence(announce, user_id=args.user_id)
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
            if not is_transient(exc) or failures >= args.max_reconnects:
                raise
            failures += 1
            wait = min(2 ** failures, 30)
            why = (f"code={exc.code}" if exc.code else
                   f"status={exc.status}" if exc.status else "no answer")
            print(f"RECONNECTING\t{why} attempt={failures} in {wait}s", flush=True)
            await asyncio.sleep(wait)


async def run(args: argparse.Namespace) -> int:
    if args.command == "stay":
        # A record, not a connection: the daemon holds the socket and outlives
        # the task that read the summon. No token, nothing kept open.
        import presence_store
        import presence_daemon
        ws = _workspace(args.workspace)
        path = presence_daemon.desired_path(ws)
        if args.leave:
            entries = presence_store.mutate_desired(
                path, lambda es: presence_store.without(es, args.room, args.kind))
            verb = "left"
        else:
            # Resolved, not raw: no flags would register identity=None and
            # name=None, and no name means a held socket nobody can see.
            who = resolve_identity(args.user_id)
            entry = {"room": args.room, "kind": args.kind,
                     "identity": who, "name": presence_name(args.name, who),
                     "summoned_at": time.time()}
            entries = presence_store.mutate_desired(
                path, lambda es: presence_store.upsert(es, entry))
            verb = "staying in"
        if args.json:
            print(json.dumps({"ok": True, "action": verb, "entries": entries},
                             ensure_ascii=False))
        else:
            print(f"{verb} {args.room} ({args.kind}); {len(entries)} surface(s) registered")
        return 0

    # Dispatched before the imports below: doctor reports missing deps as its
    # own first step, and importing the client here would exit before it runs.
    if args.command == "doctor":
        return await doctor(args)

    # Imported here, not at module scope: the rules above are pure, and a test
    # of them must not need pycrdt installed.
    from room_collab_client import open_room_collab

    from room_collab_board import BOARD_KIND, place_clear
    from room_kanban import KANBAN_KIND
    if args.command == "summon":
        # No document connection: a summon is a room message, and its context is
        # what the caller states rather than a passage this command verifies.
        body, extra = summon_content(args.room, args.invitee, args.kind, args.context)
        if args.dry_run:
            print(json.dumps({"room": args.room, "body": body, "extra_content": extra},
                             ensure_ascii=False, indent=2))
            return 0
        receipt = post_summon(args.room, body, extra)
        if args.json:
            print(json.dumps(receipt, ensure_ascii=False))
        else:
            print(f"summoned {args.invitee} to the {args.kind} surface: "
                  f"{receipt.get('event_id') or receipt.get('state') or 'posted'}")
        return 0

    if args.command == "reply":
        # No document at all: a reply is a room message in the comment's thread.
        body = reply_content(args.text, args.mention)
        if args.dry_run:
            print(json.dumps({"room": args.room, "thread_root": args.event, "body": body},
                             ensure_ascii=False, indent=2))
            return 0
        receipt = post_reply(args.room, args.event, body)
        print(json.dumps(receipt, ensure_ascii=False) if args.json else
              f"replied under {args.event}: {receipt.get('event_id') or receipt.get('state') or 'posted'}")
        return 0

    token, url = resolve_token(args.token), resolve_url(args.url)
    if args.command == "presence":
        # No socket: opening one would put this agent in the count it asks for.
        print(render_presence(args.room, presence_summary(url, args.room, token), args.json))
        return 0
    if args.command == "watch":
        return await watch(args, token, url)
    if args.command == "script":
        from room_collab_relay import read_script
        print(json.dumps(await read_script(lambda: open_room_collab(url, args.room, token,
                                                                    insecure=args.insecure)),
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "relay":
        from room_collab_relay import serve
        await serve(lambda room: open_room_collab(url, room, token, kind=args.kind,
                                                  insecure=args.insecure), args.port,
                    room=args.room,
                    open_text=lambda room: open_room_collab(url, room, token,
                                                            insecure=args.insecure),
                    list_rooms=joined_rooms)
        return 0

    async with open_room_collab(url, args.room, token, kind=args.kind,
                             insecure=args.insecure) as doc:
        if args.name:
            await doc.set_presence(args.name, user_id=args.user_id)

        if args.kind == KANBAN_KIND:
            return await kanban(doc, args)
        if args.kind == "sheet":
            return await sheet(doc, args)

        if args.kind == BOARD_KIND:
            # Presence is its own channel and belongs to no surface, so
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
        if args.command == "templates":
            return await templates(doc, args, url)
        if args.command == "slide":
            if args.kind != HTML_KIND:
                raise RoomDocError(f"slide is for the HTML page: pass --kind {HTML_KIND}.")
            move = args.move.lower()
            nav = await (doc.navigate("goto", int(move)) if move.isdigit() else doc.navigate(move))
            await doc.settle(args.settle)
            print(json.dumps({"ok": True, **nav}))
            return 0
        if args.command == "highlight":
            if args.kind != HTML_KIND:
                raise RoomDocError(f"highlight is for the HTML page: pass --kind {HTML_KIND}.")
            state = await doc.set_stage(None if args.topic == "clear" else args.topic)
            await doc.settle(args.settle)
            print(json.dumps({"ok": True, **state}))
            return 0
        if args.command == "comment":
            if args.kind != DEFAULT_KIND:
                raise RoomDocError(f"comments are pinned to the Doc; the {args.kind!r} page "
                                   "has no comment layer yet. Say it in the room instead.")
            at, nth = locate_quote(doc.text, args.quote, args.nth)
            body, extra = comment_content(doc.anchor(at, at + len(args.quote)), args.quote, nth,
                                          args.text, args.mention)
            if args.dry_run:
                print(json.dumps({"room": args.room, "body": body, "extra_content": extra},
                                 ensure_ascii=False, indent=2))
                return 0
            receipt = post_comment(args.room, body, extra)
            if args.json:
                print(json.dumps(receipt, ensure_ascii=False))
            else:
                print(f"commented on {args.quote[:60]!r} (occurrence {nth}): "
                      f"{receipt.get('event_id') or receipt.get('state') or 'posted'}")
            return 0
        before = len(doc.text)
        if args.command == "append":
            await doc.append(args.text)
            await doc.settle(args.settle)
        elif args.command == "replace":
            await doc.replace(args.old, args.new)
            await doc.settle(args.settle)
        if args.command == "read":
            # Every read remembers what it saw, so the next `--delta` is literal.
            snap = snapshot_path(_workspace(getattr(args, "workspace", None)), args.room, args.kind,
                                 reader_identity(args))
            previous, seen_at = recall(snap)
            remember(snap, doc.text)
            if getattr(args, "delta", False):
                lines = delta_since(previous, doc.text)
                since = (time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(seen_at))
                         if seen_at else None)
                if args.json:
                    print(json.dumps({"chars": len(doc.text), "since": since, "delta": lines,
                                      "peers": doc.peers}, ensure_ascii=False, indent=2))
                else:
                    head = (f"{len(lines)} new line(s) since your last read at {since}" if since
                            else f"no earlier read of this surface — everything is new ({len(lines)} line(s))")
                    print(head + ("\n" + "\n".join(lines) if lines else ""))
                return 0
        print(render(args.command, text=doc.text, peers=doc.peers,
                     as_json=args.json, before=before,
                     authors=doc.authors if args.with_authors else None))
    return 0


def locate_quote(text: str, quote: str, nth: int | None = None) -> tuple[int, int]:
    """Where `quote` sits in `text`: (character offset, occurrence index).
    Refuses an absent quote, and an ambiguous one unless `nth` picks."""
    if not quote:
        raise RoomDocError("a comment needs the text it is on: the quote is empty")
    if len(quote) > QUOTE_MAX:
        raise RoomDocError(f"the quote is {len(quote)} chars; a comment anchors to at most "
                           f"{QUOTE_MAX} — quote less of the passage")
    hits = []
    i = text.find(quote)
    while i != -1:
        hits.append(i)
        i = text.find(quote, i + 1)
    if not hits:
        raise RoomDocError(f"the quoted text is not in the document: {quote[:60]!r}")
    if nth is None:
        if len(hits) > 1:
            raise RoomDocError(f"{quote[:60]!r} occurs {len(hits)} times; pass --nth 0..{len(hits) - 1} "
                               "(0 is the first) or quote more of the passage")
        nth = 0
    if not 0 <= nth < len(hits):
        raise RoomDocError(f"--nth {nth}, but {quote[:60]!r} occurs {len(hits)} time(s)")
    return hits[nth], nth


def comment_content(anchor: dict, quote: str, nth: int, message: str,
                    mentions: list[str] | None = None) -> tuple[str, dict]:
    """The room message a comment is: a body any client can read, and the
    anchor the collab client hangs it on. The quote leads the body so a plain
    timeline shows what is being talked about."""
    text = message.strip()
    if not text:
        raise RoomDocError("a comment needs something to say")
    # A full mxid in the body is what the gateway turns into a real mention.
    lead = " ".join(m for m in (mentions or []) if m)
    body = f"> {quote}\n\n{lead + ' ' + text if lead else text}"
    return body, {COMMENT_KEY: {"anchor": {**anchor, "quote": quote, "nth": nth}, "v": 1}}


def reply_content(message: str, mentions: list[str] | None = None) -> str:
    """A reply's body: the words as they are, no quote in front — the thread it
    sits in already says what it is about, and a card shows the body verbatim."""
    text = message.strip()
    if not text:
        raise RoomDocError("a reply needs something to say")
    lead = " ".join(m for m in (mentions or []) if m)
    return lead + " " + text if lead else text


def summon_content(room: str, invitee: str, kind: str,
                   context: str | None = None) -> tuple[str, dict]:
    """The room message a summon is: prose any client shows, and the marker the
    collab client renders as the summon card.

    v3 — `invitee` and `context` in the marker, so a reader draws the card
    without parsing the prose. v1/v2 carried neither and the client falls back
    to `m.mentions`; the full mxid in the body is what makes that mention real.
    """
    who = invitee.strip()
    if not MXID_RE.match(who):
        raise RoomDocError(f"a summon needs the mxid of whoever is called, like "
                           f"@name:server — got {invitee!r}")
    where = SUMMON_SURFACE.get(kind)
    if where is None:
        raise RoomDocError(f"{kind!r} is not a surface to summon anyone to; "
                           f"use one of {', '.join(sorted(SUMMON_SURFACE))}")
    quoted = " ".join((context or "").split())[:SUMMON_CONTEXT_MAX]
    body = f"{who} — you're needed in this room's {where}."
    if quoted:
        body += f"\n\n> {quoted}"
    marker = {"room_id": room, "kind": kind, "invitee": who, "v": 3}
    if quoted:
        marker["context"] = quoted
    return body, {SUMMON_KEY: marker, "m.mentions": {"user_ids": [who]}}


def post_summon(room: str, body: str, extra: dict, *, runner=subprocess.run,
                script: Path | None = None) -> dict:
    """Post the summon through room-ops `say`; the reply is its receipt."""
    return _post(room, body, ["--extra-content", json.dumps(extra, ensure_ascii=False)],
                 "summon", runner=runner, script=script)


def post_reply(room: str, root: str, body: str, *, runner=subprocess.run,
               script: Path | None = None) -> dict:
    """Post a reply in the comment's thread through room-ops `say --thread-root`."""
    root = root.strip()
    if not root.startswith("$") or len(root) < 2:
        raise RoomDocError(f"a reply goes under a comment's event id, like $abc — got {root!r}")
    return _post(room, body, ["--thread-root", root], "reply", runner=runner, script=script)


def room_ops_script() -> Path | None:
    """The room-ops skill installed beside this one, which is how an agent posts
    a room message; None when it is not there."""
    cand = HERE.parent.parent / "agent-room-ops" / "room_ops.py"
    return cand if cand.is_file() else None


def joined_rooms(*, runner=subprocess.run, script: Path | None = None) -> list[dict]:
    """The agent's joined rooms as [{id, name}], through room-ops `rooms`: the
    gateway's list is that skill's, and a skill does not import another's code."""
    script = script or room_ops_script()
    if script is None:
        raise RoomDocError("listing rooms needs the agent-room-ops skill installed beside this one")
    try:
        proc = runner([sys.executable, str(script), "rooms"], capture_output=True, text=True,
                      timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoomDocError(f"could not list rooms: {exc}") from exc
    try:
        res = json.loads(proc.stdout or "")
    except ValueError:
        res = {}
    if not isinstance(res, dict) or not res.get("ok"):
        why = (res.get("reason") if isinstance(res, dict) else None) or \
            (proc.stderr or proc.stdout or "").strip()[-300:] or f"exit {proc.returncode}"
        raise RoomDocError(f"could not list rooms: {why}")
    named = {r.get("room_id"): r.get("name") for r in res.get("rooms_detailed") or []
             if isinstance(r, dict)}
    ids = list(dict.fromkeys([*(res.get("rooms") or []), *named]))
    return [{"id": i, "name": named.get(i) or None} for i in ids if isinstance(i, str) and i]


def post_comment(room: str, body: str, extra: dict, *, runner=subprocess.run,
                 script: Path | None = None) -> dict:
    """Post the comment through room-ops `say`; the reply is its receipt."""
    return _post(room, body, ["--extra-content", json.dumps(extra, ensure_ascii=False)],
                 "comment", runner=runner, script=script)


def _post(room: str, body: str, flags: list[str], what: str, *, runner, script) -> dict:
    script = script or room_ops_script()
    if script is None:
        raise RoomDocError("posting needs the agent-room-ops skill installed beside this one. "
                           "Rerun with --dry-run and post that content with `room_ops.py say "
                           f"{flags[0]}` yourself.")
    proc = runner([sys.executable, str(script), "say", room, body, *flags],
                  capture_output=True, text=True)
    try:
        receipt = json.loads(proc.stdout or "")
    except ValueError:
        receipt = {}
    if proc.returncode != 0 or not isinstance(receipt, dict) or not receipt.get("ok"):
        why = (receipt.get("reason") if isinstance(receipt, dict) else None) or \
            (proc.stderr or proc.stdout or "").strip()[-300:] or f"exit {proc.returncode}"
        raise RoomDocError(f"the {what} was not posted: {why}")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="room_collab", description=__doc__)
    p.add_argument("--url", help="service origin (else $AG2_ROOM_COLLAB_URL, $AG2_API_ROOT, or the relay's)")
    p.add_argument("--token", help="bearer; the relay token works (else the env, see SKILL.md)")
    p.add_argument("--name", help="presence name to publish while connected")
    p.add_argument("--user-id", dest="user_id", default=None,
                   help="this agent's mxid, so the roster can show its avatar")
    p.add_argument("--kind", default="markdown",
                   help="which of the room's surfaces (markdown, html, board, kanban, sheet); "
                        "default markdown")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (local rig only)")
    p.add_argument("--settle", type=float, default=1.0, help="seconds to wait after a write")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--with-authors", dest="with_authors", action="store_true",
                   help="also report who wrote with each Yjs client id")
    sub = p.add_subparsers(dest="command", required=True)

    p.add_argument("--workspace", default=None,
                   help="workspace root for what you last read (default: the repo's resolver)")
    for name, help_text in (("read", "print the document"), ("peers", "who is present"),
                            ("doctor", "check deps, credential, URL and connection, step by step"),
                            ("presence", "who is in each of the room's surfaces, without opening any")):
        s = sub.add_parser(name, help=help_text)
        if name == "read":
            s.add_argument("--delta", action="store_true",
                           help="only the lines new since this agent last read the surface")
        s.add_argument("room", help="Matrix room id, e.g. !abc:server")

    s = sub.add_parser("stay",
                       help="register this agent as resident in a surface; the presence daemon "
                            "holds the connection and outlives this process")
    s.add_argument("room")
    s.add_argument("--leave", action="store_true",
                   help="deregister instead: the daemon drops the connection on its next pass")

    s = sub.add_parser("watch", help="hold the surface open; print each event that concerns --for")
    s.add_argument("room")
    s.add_argument("--for", dest="handles", action="append", metavar="HANDLE",
                   help="a name or @mxid to watch for (repeatable); default: your mxid, its "
                        "localpart and --name. Add the display name a summon shows for you.")
    s.add_argument("--max-reconnects", type=int, default=20,
                   help="give up after this many consecutive failed reconnects")

    s = sub.add_parser("append", help="append text to the end")
    s.add_argument("room")
    s.add_argument("text")

    s = sub.add_parser("replace", help="replace the first occurrence of some text")
    s.add_argument("room")
    s.add_argument("old")
    s.add_argument("new")

    s = sub.add_parser("highlight", help="highlight a topic on the HTML page for everyone "
                                         "watching; `clear` removes it (needs --kind html)")
    s.add_argument("room")
    s.add_argument("topic", help="a data-topic key the page defines, or `clear`")

    s = sub.add_parser("set", help="set one sheet cell to a value or =formula (needs --kind sheet)")
    s.add_argument("room")
    s.add_argument("cell", help="an address like B4")
    s.add_argument("value", help='the input as typed, e.g. 42, "Q3", or =SUM(B1:B9)')

    s = sub.add_parser("import", help="write a CSV file into the sheet from --at (needs --kind sheet)")
    s.add_argument("room")
    s.add_argument("file")
    s.add_argument("--at", default="A1", help="the top-left cell (default A1)")

    s = sub.add_parser("slide", help="move every viewer's deck: next, prev, or a slide number "
                                     "(needs --kind html)")
    s.add_argument("room")
    s.add_argument("move", help="next | prev | <slide number>")

    s = sub.add_parser("script", help="print the talk script in the room's Doc as steps of "
                                      "words and cues (JSON)")
    s.add_argument("room")

    s = sub.add_parser("relay", help="hold the HTML page open and serve the local talk-highlight "
                                     "API on 127.0.0.1, for a voice agent (needs --kind html)")
    s.add_argument("room", help="the room to hold first; POST /room/<id> switches it")
    s.add_argument("--port", type=int, default=7877)

    s = sub.add_parser("templates", help="list the HTML page templates, or start the page from one "
                                         "(needs --kind html)")
    s.add_argument("room")
    s.add_argument("--use", metavar="ID", help="replace the page with this template")
    s.add_argument("--replace", action="store_true",
                   help="allow --use to overwrite a page that already has content")
    s.add_argument("--library", help="the library's base URL (default: <service>/html-templates/)")

    s = sub.add_parser("comment", help="comment on a passage of the document, pinned to those words")
    s.add_argument("room")
    s.add_argument("quote", help="the exact text the comment is on, as it appears in the document")
    s.add_argument("text", help="what to say about it")
    s.add_argument("--nth", type=int, default=None,
                   help="which occurrence of the quote, 0-based, when it appears more than once")
    s.add_argument("--mention", action="append", default=[], metavar="MXID",
                   help="address someone by mxid (repeatable); an agent among them is called")
    s.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="print the room message the comment would be and post nothing")

    s = sub.add_parser("reply", help="answer in a comment's thread (no document connection needed)")
    s.add_argument("room")
    s.add_argument("event", help="the comment's event id ($abc), from its receipt or the room")
    s.add_argument("text", help="what to say")
    s.add_argument("--mention", action="append", default=[], metavar="MXID",
                   help="address someone by mxid (repeatable); an agent among them is called")
    s.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="print the room message the reply would be and post nothing")

    s = sub.add_parser("summon",
                       help="call someone into a surface — the card the client renders "
                            "(no document connection needed)")
    s.add_argument("room")
    s.add_argument("invitee", metavar="MXID",
                   help="who is called, by mxid — a person or another agent")
    s.add_argument("--context", default=None,
                   help="the passage they are called about, quoted under the card; "
                        "stated by you, not checked against the document")
    s.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="print the room message the summon would be and post nothing")

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
