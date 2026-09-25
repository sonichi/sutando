"""A local HTTP relay onto a room surface's shared stage: the HTML page, the board or the Doc.

A voice agent's tool must answer within a turn; opening the room per call costs
seconds on a large page. The relay holds one connection and speaks the local
talk-highlight API, so an existing voice tool drives the room's page unchanged:

  POST /highlight/<topic>   (topic `clear` clears)
  POST /slide/next|prev|<n> move every viewer's deck
  POST /speaking/on|off
  POST /presenter/on|off    accepted; presenting is the room's, not the relay's
  GET  /state               {topic, ts, speaking}
  POST /spot/<words>        spotlight the passage with these words (`clear` clears)
  GET  /script              the talk script in the room's Doc, as steps (talk_script.py)
  GET  /outline             the page's slides, titles and topics (page_outline.py); the
                            board's frames or the Doc's headings (surface_outline.py)
  POST /surface/html|board|doc|db  hold that surface instead (html at start); GET /surface names it
  POST /surface/html-<id>   hold one of the room's extra HTML pages (also POST /page/<id>;
                            /page/main is the main page)
  GET  /pages               the room's HTML pages: main first, then each extra page's id and title
  GET  /appstate            the held page's shared state (what its scripts see as artifact.state)
  POST /appstate/<key>/<json>  set one key of it (url-encoded JSON; `null` deletes)
  POST /room/<room id>      hold that room instead (url-encoded `!abc:server`), same surface
  GET  /rooms               the agent's joined rooms, {id, name}, to resolve a spoken name
  GET  /db                  the room's databases (room_database.py)
  GET  /db/<db>[/view/<view>]  a view's rows with display values; db and view by name or id
  POST /db/<db>/row?set=Prop%3DValue&set=...  add a row, values by property name
  POST /db/<db>/row/<row>?set=...             set values on a row (by id or title)
  POST /db/<db>/move?row=<row>&to=<group>     move a row on the board view (&view=<name>)
  GET  /db/<db>/row/<row>   the row as a page: its properties by name, then its markdown body
  POST /db/<db>/row/<row>/body?text=...[&append=1]  set (or append to) the row page's body

On the board a slide is a frame (in Present order) and on the Doc a heading;
topic highlights exist only on the page. The /db routes use the held database surface,
or open the room's databases for the one call when another surface is held.

It listens on 127.0.0.1 only: whoever reaches the port drives the stage as
this agent, so it must never be exposed beyond the machine.
"""
from __future__ import annotations

import asyncio
import json
import re

from room_collab_protocol import DEFAULT_KIND, STATE_KEY_RE, RoomDocError, is_html_kind

TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECONNECT_S = (1, 2, 5, 10, 30)
SURFACES = {"html": "html", "board": "board", "doc": DEFAULT_KIND, "db": "db"}
DB_NAME_MAX, DB_SET_MAX = 200, 2000
# A body rides the query string: the request line must stay under the reader's limit.
DB_BODY_MAX, LINE_LIMIT = 16_000, 1 << 18
PAGE_ID_RE = re.compile(r"[a-z0-9]{8}")


def surface_kind(surface: str) -> str | None:
    """The document kind a relay surface opens: a named one, or an extra page's own `html-<id>`."""
    return SURFACES.get(surface) or (surface if is_html_kind(surface) else None)
SWITCH_WAIT_S = 15
ROOM_ID_RE = re.compile(r"![^\s:/]+:[^\s/]+")


def visible_words(html: str) -> str:
    """The page's words, lower-cased and single-spaced, to tell a speaker whether a spotlight will land."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>", " ", html)
    import html as _html
    return " ".join(_html.unescape(text).lower().split())


def route(method: str, path: str) -> tuple[str, object] | tuple[int, dict]:
    """What a request asks for, as ("highlight", topic|None), ("speaking", bool),
    ("state", None), or an HTTP status and body to answer directly."""
    raw_query = path.split("?", 1)[1] if "?" in path else ""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    if path == "/db" or path.startswith("/db/"):
        return db_route(method, path, raw_query)
    if method == "GET" and path == "/state":
        return ("state", None)
    if method == "GET" and path == "/script":
        return ("script", None)
    if method == "GET" and path == "/outline":
        return ("outline", None)
    if method == "GET" and path == "/surface":
        return ("surface", None)
    if method == "GET" and path == "/rooms":
        return ("rooms", None)
    if method == "GET" and path == "/pages":
        return ("pages", None)
    if method == "GET" and path == "/appstate":
        return ("appstate", None)
    if method != "POST":
        return (405 if path.startswith(("/highlight/", "/slide/", "/spot/", "/speaking/", "/presenter/",
                                        "/surface/", "/room/", "/page/"))
                else 404,
                {"ok": False, "error": "not found"})
    if path.startswith("/highlight/"):
        topic = path[len("/highlight/"):].lower()
        if topic == "clear":
            return ("highlight", None)
        if not TOPIC_RE.fullmatch(topic):
            return (400, {"ok": False, "error": f"not a topic key: {topic!r}"})
        return ("highlight", topic)
    if path.startswith("/room/"):
        from urllib.parse import unquote
        room = unquote(path[len("/room/"):]).strip()
        if len(room) > 255 or not ROOM_ID_RE.fullmatch(room):
            return (400, {"ok": False, "error": f"not a room id: {room[:80]!r} (like !abc:server)"})
        return ("room", room)
    if path.startswith("/appstate/"):
        from urllib.parse import unquote
        key, _, raw = path[len("/appstate/"):].partition("/")
        if not re.fullmatch(STATE_KEY_RE, key) or not raw:
            return (400, {"ok": False, "error": "use /appstate/<key>/<url-encoded JSON>; `null` deletes"})
        try:
            return ("appstate_set", (key, json.loads(unquote(raw))))
        except ValueError:
            return (400, {"ok": False, "error": "the value must be JSON (quote strings)"})
    if path.startswith("/spot/"):
        from urllib.parse import unquote
        words = " ".join(unquote(path[len("/spot/"):]).split())
        if words.lower() == "clear":
            return ("spot", None)
        if not words or len(words) > 200:
            return (400, {"ok": False, "error": "a spotlight needs 1–200 characters of the page's words"})
        return ("spot", words)
    if path.startswith("/slide/"):
        what = path[len("/slide/"):]
        if what in ("next", "prev"):
            return ("slide", (what, None))
        if what.isdigit() and 1 <= int(what) <= 999:
            return ("slide", ("goto", int(what)))
        return (400, {"ok": False, "error": f"not a move: {what!r} (next, prev or a slide number)"})
    if path in ("/speaking/on", "/speaking/off"):
        return ("speaking", path.endswith("/on"))
    if path.startswith("/surface/"):
        what = path[len("/surface/"):].lower()
        if surface_kind(what) is None:
            return (400, {"ok": False, "error": f"not a surface: {what!r} (html, html-<id>, board, doc or db)"})
        return ("surface", what)
    if path.startswith("/page/"):
        what = path[len("/page/"):].lower()
        if what == "main":
            return ("surface", "html")
        if not PAGE_ID_RE.fullmatch(what):
            return (400, {"ok": False, "error": f"not a page id: {what!r} (8 of a-z0-9, or main)"})
        return ("surface", f"html-{what}")
    if path in ("/presenter/on", "/presenter/off"):
        return (200, {"ok": True, "note": "presenting is controlled in the room"})
    return (404, {"ok": False, "error": "not found"})


def db_route(method: str, path: str, raw_query: str) -> tuple:
    """/db requests → ("db", {op, db, view, row, set, to}) or a status and body."""
    from urllib.parse import parse_qs, unquote
    segs = [unquote(x) for x in path.strip("/").split("/")][1:]
    q = parse_qs(raw_query, keep_blank_values=True)
    if any(len(x) > DB_NAME_MAX for x in segs) or any(
            len(x) > (DB_BODY_MAX if k == "text" else DB_SET_MAX) for k, v in q.items() for x in v):
        return (400, {"ok": False, "error": "a name or value is too long"})
    reads = {0: "list", 1: "view"}
    if len(segs) == 3 and segs[1] == "row" and method == "GET":
        return ("db", {"op": "row", "db": segs[0], "row": segs[2]})
    if len(segs) == 4 and segs[1] == "row" and segs[3] == "body":
        if method != "POST":
            return (405, {"ok": False, "error": "not found"})
        if "text" not in q:
            return (400, {"ok": False, "error": "a body write needs text=<markdown> (empty clears it)"})
        return ("db", {"op": "body", "db": segs[0], "row": segs[2], "text": q["text"][0],
                       "append": (q.get("append") or [""])[0] in ("1", "true", "yes")})
    if method == "GET":
        if len(segs) in reads or (len(segs) == 3 and segs[1] == "view"):
            return ("db", {"op": reads.get(len(segs), "view"), "db": segs[0] if segs else None,
                           "view": segs[2] if len(segs) == 3 else None})
        return (405 if len(segs) >= 2 and segs[1] in ("row", "move") else 404, {"ok": False, "error": "not found"})
    if method != "POST":
        return (405, {"ok": False, "error": "not found"})
    sets = q.get("set", [])
    if any("=" not in x for x in sets):
        return (400, {"ok": False, "error": "each set is Property=Value (url-encode the = as %3D)"})
    if len(segs) == 2 and segs[1] == "row":
        return ("db", {"op": "add", "db": segs[0], "set": sets})
    if len(segs) == 3 and segs[1] == "row":
        if not sets:
            return (400, {"ok": False, "error": "an update needs at least one set=Property=Value"})
        return ("db", {"op": "update", "db": segs[0], "row": segs[2], "set": sets})
    if len(segs) == 2 and segs[1] == "move":
        row, to = (q.get("row") or [""])[0], (q.get("to") or [""])[0]
        if not row or not to:
            return (400, {"ok": False, "error": "a move needs row=<row id or title> and to=<group>"})
        return ("db", {"op": "move", "db": segs[0], "row": row, "to": to,
                       "view": (q.get("view") or [None])[0]})
    return (405 if len(segs) <= 1 else 404, {"ok": False, "error": "not found"})


async def db_request(doc, req: dict, identity=None) -> tuple[int, dict]:
    """One /db request against an open database surface; a refusal is a 400 and writes nothing."""
    from room_database import (DbRefusal, add_row_plan, assignments, cell_writes, group_target, list_dbs,
                               move_plan, read_db, resolve_db, resolve_prop, resolve_row, resolve_view,
                               row_json, view_json)
    maps = doc.database
    names = {x["id"]: x["name"] for x in list_dbs(maps)}
    try:
        if req["op"] == "list":
            return 200, {"ok": True, "databases": [
                {**x, "rows": len(read_db(maps, x["id"])["rows"]),
                 "views": [v["name"] for v in read_db(maps, x["id"])["views"]]} for x in list_dbs(maps)]}
        d = resolve_db(maps, req["db"])
        if req["op"] == "view":
            return 200, {"ok": True, **view_json(d, resolve_view(d, req.get("view")), names[d["id"]])}
        if req["op"] == "row":
            row = resolve_row(d, req["row"])
            return 200, {"ok": True, **row_json(d, row, doc.row_body(d["id"], row["id"]), names[d["id"]])}
        if identity is None:
            raise RoomDocError("this relay was started without an identity to sign writes (--user-id)")
        by = identity()
        body: dict = {"ok": True, "db": d["id"], "name": names[d["id"]]}
        if req["op"] == "body":
            row = resolve_row(d, req["row"])["id"]
            chars = await doc.put_row_body(d["id"], row, req["text"], append=req["append"])
            return 200, {"ok": True, "db": d["id"], "name": names[d["id"]], "row": row, "chars": chars,
                         "appended" if req["append"] else "set": True}
        if req["op"] == "add":
            row, writes = add_row_plan(d, by, assignments(d, req["set"]))
        elif req["op"] == "update":
            row = resolve_row(d, req["row"])["id"]
            writes = {"cells": cell_writes(d, row, assignments(d, req["set"]), by)}
        else:
            view = resolve_view(d, req.get("view"), layout="board")
            prop = resolve_prop(d, view.get("groupBy") or "")
            row = resolve_row(d, req["row"])["id"]
            writes = move_plan(d, row, prop, group_target(d, prop, req["to"]), by)
            body.update(view=view["name"], to=req["to"])
        body.update(row=row, written=await doc.put_database(writes))
        return 200, body
    except DbRefusal as exc:
        return 400, {"ok": False, "error": str(exc)}


async def read_script(open_text) -> dict:
    """The room Doc's talk script, parsed; opened per call since it is read once per talk."""
    from talk_script import extract, parse
    async with open_text() as doc:
        section = extract(doc.text)
    if section is None:
        raise RoomDocError('the room\'s Doc has no "Talk script" heading')
    return {"ok": True, "steps": parse(section)}


def outline_of(surface: str, doc) -> dict:
    if surface == "db":
        from room_database import list_dbs
        return {"kind": "db", "databases": [{"id": x["id"], "name": x["name"]} for x in list_dbs(doc.database)]}
    if surface == "board":
        from surface_outline import board_outline
        return board_outline(doc.live_elements)
    if surface == "doc":
        from surface_outline import doc_outline
        return doc_outline(doc.text)
    from page_outline import outline
    return outline(doc.text)


def says(surface: str, doc, words: str) -> bool:
    """Whether a spotlight on these words will land on the surface."""
    want = " ".join(words.lower().split())
    if surface == "board":
        from surface_outline import board_words
        return want in board_words(doc.live_elements)
    if surface == "doc":
        return want in " ".join(doc.text.lower().split())
    return want in visible_words(doc.text)


async def serve(open_doc, port: int, *, room: str, host: str = "127.0.0.1", log=print,
                open_text=None, open_kind=None, list_rooms=None, identity=None) -> None:
    """Hold one surface of one room open (reconnecting when it drops) and answer HTTP on `port`.
    `open_doc(room)` opens a room's page, `open_kind(room, kind)` another of its surfaces,
    `open_text(room)` its Doc for GET /script; `list_rooms()` (blocking) lists joined rooms;
    `identity()` is the mxid a /db write is signed with."""
    holder: dict = {"doc": None, "room": room, "surface": "html", "error": None}
    ready = asyncio.Event()
    switch = asyncio.Event()

    def target() -> tuple[str, str]:
        return holder["room"], holder["surface"]

    async def keep_connected() -> None:
        attempt = 0
        while True:
            switch.clear()
            held = target()
            held_room, surface = held
            if surface == "html":
                opener = lambda: open_doc(held_room)  # noqa: E731
            else:
                opener = lambda: open_kind(held_room, surface_kind(surface))  # noqa: E731
            try:
                async with opener() as doc:
                    if surface == "html" and not is_html_kind(doc.kind):
                        raise RoomDocError("the relay drives the HTML page: open it with --kind html")
                    # A switch asked for while this one opened: go straight to the new target.
                    if target() == held:
                        holder["doc"], holder["error"], attempt = doc, None, 0
                        ready.set()
                        size = (f"{len(doc.live_elements)} elements" if surface == "board"
                                else f"{len(doc.database['dbs'])} databases" if surface == "db"
                                else f"{len(doc.text)} chars")
                        log(f"relay: connected to the {surface} of {held_room}; {size}")
                        closed = asyncio.ensure_future(doc.closed())
                        switched = asyncio.ensure_future(switch.wait())
                        await asyncio.wait({closed, switched}, return_when=asyncio.FIRST_COMPLETED)
                        closed.cancel()
                        switched.cancel()
            except RoomDocError as exc:
                log(f"relay: {exc}")
                holder["error"] = str(exc)
                if "--kind html" in str(exc):
                    raise
            holder["doc"] = None
            ready.clear()
            if switch.is_set() or target() != held:
                attempt = 0
                continue
            delay = RECONNECT_S[min(attempt, len(RECONNECT_S) - 1)]
            attempt += 1
            log(f"relay: reconnecting in {delay}s")
            try:
                await asyncio.wait_for(switch.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def retarget(room_id: str | None = None, surface: str | None = None) -> None:
        new = (room_id or holder["room"], surface or holder["surface"])
        if new != target():
            log(f"relay: switching to the {new[1]} of {new[0]}")
            holder.update(room=new[0], surface=new[1], doc=None, error=None)
            ready.clear()
            switch.set()

    async def change_surface(to: str | None) -> dict:
        if to is not None and to != holder["surface"]:
            if to != "html" and open_kind is None:
                raise RoomDocError("this relay was started without the room's other surfaces")
            retarget(surface=to)
        try:
            await asyncio.wait_for(ready.wait(), SWITCH_WAIT_S if to else 0.01)
        except asyncio.TimeoutError:
            if to:
                raise RoomDocError(f"could not open the room's {to} yet") from None
            return {"ok": True, "surface": holder["surface"], "connected": False}
        o = outline_of(holder["surface"], holder["doc"])
        return {"ok": True, "surface": holder["surface"], "connected": True,
                "parts": len(o.get("slides") or o.get("headings") or o.get("databases") or [])}

    async def switch_room(room_id: str) -> dict:
        retarget(room_id=room_id)
        try:
            await asyncio.wait_for(ready.wait(), SWITCH_WAIT_S)
        except asyncio.TimeoutError:
            return {"ok": True, "room": room_id, "connected": False,
                    "error": holder["error"] or "not connected to the room yet; still trying"}
        if holder["doc"] is None or holder["room"] != room_id:
            return {"ok": False, "room": holder["room"], "error": "another switch overtook this one"}
        doc, surface = holder["doc"], holder["surface"]
        body = {"ok": True, "room": room_id, "surface": surface, "connected": True}
        if is_html_kind(surface):
            body.update(has_page=bool(doc.text.strip()), chars=len(doc.text))
        return body

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, body = 500, {"ok": False}
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.split()
            what = route(parts[0], parts[1]) if len(parts) >= 2 else (400, {"ok": False})
            if isinstance(what[0], int):
                status, body = what
            elif what[0] == "script":
                if open_text is None:
                    raise RoomDocError("this relay was started without the room's Doc")
                current = holder["room"]
                status, body = 200, await read_script(lambda: open_text(current))
            elif what[0] == "surface":
                status, body = 200, await change_surface(what[1])
            elif what[0] == "room":
                status, body = 200, await switch_room(what[1])
            elif what[0] == "db":
                if holder["surface"] == "db" and holder["doc"] is not None:
                    status, body = await db_request(holder["doc"], what[1], identity)
                elif open_kind is None:
                    raise RoomDocError("this relay was started without the room's other surfaces")
                else:
                    async with open_kind(holder["room"], SURFACES["db"]) as doc:
                        status, body = await db_request(doc, what[1], identity)
                        if what[1]["op"] not in ("list", "view", "row") and status == 200:
                            await doc.settle(0.3)  # a refusal from the server surfaces before close
                body["room"] = holder["room"]
            elif what[0] == "pages":
                if open_kind is None:
                    raise RoomDocError("this relay was started without the room's other surfaces")
                async with open_kind(holder["room"], "html") as main:
                    listed = [{"id": None, "title": "Main"}] + [
                        {"id": p["id"], "title": p["title"]} for p in main.pages]
                status, body = 200, {"ok": True, "pages": listed, "surface": holder["surface"]}
            elif what[0] == "rooms":
                if list_rooms is None:
                    raise RoomDocError("this relay was started without a room list")
                rooms = await asyncio.to_thread(list_rooms)
                status, body = 200, {"ok": True, "rooms": rooms, "current": holder["room"]}
            else:
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                except asyncio.TimeoutError:
                    raise RoomDocError("not connected to the room yet") from None
                doc, surface = holder["doc"], holder["surface"]
                if doc is None:
                    raise RoomDocError("the relay is switching; try again")
                kind, arg = what
                parts = None
                if kind == "slide" and arg[0] == "goto" and not is_html_kind(surface):
                    o = outline_of(surface, doc)
                    parts = len(o.get("slides") or o.get("headings") or [])
                if surface == "db" and kind in ("slide", "spot", "speaking"):
                    status, body = 409, {"ok": False, "error": "the databases have no stage to move or point "
                                         "at; switch with /surface/html, board or doc"}
                elif kind == "highlight" and not is_html_kind(surface):
                    status, body = 409, {"ok": False, "error": f"topic highlights are on the HTML page; "
                                         f"the relay holds the {surface} — point at words with /spot"}
                elif kind == "highlight":
                    state = await doc.set_stage(arg)
                    status, body = 200, {"ok": True, **state}
                elif kind == "spot":
                    spot = await doc.set_spot(arg)
                    found = arg is None or says(surface, doc, arg)
                    status, body = 200, {"ok": True, **spot, "found_on_page": found}
                elif kind == "outline":
                    status, body = 200, {"ok": True, **outline_of(surface, doc)}
                elif kind in ("appstate", "appstate_set") and not is_html_kind(surface):
                    status, body = 409, {"ok": False, "error": "page state is on an HTML page; "
                                         "switch with /surface/html or /page/<id>"}
                elif kind == "appstate":
                    status, body = 200, {"ok": True, "state": doc.app_state, "surface": surface}
                elif kind == "appstate_set":
                    await doc.set_app_state(*arg)
                    status, body = 200, {"ok": True, "key": arg[0], "deleted": arg[1] is None}
                elif kind == "slide" and parts is not None and arg[1] > parts:
                    noun = "frames" if surface == "board" else "headings"
                    status, body = 400, {"ok": False, "error": f"the {surface} has {parts} {noun}"}
                elif kind == "slide":
                    nav = await doc.navigate(*arg)
                    status, body = 200, {"ok": True, **nav}
                elif kind == "speaking":
                    await doc.set_speaking(bool(arg))
                    status, body = 200, {"ok": True, "speaking": bool(arg)}
                elif surface == "db":
                    status, body = 200, {"topic": None, "ts": 0, "speaking": False,
                                         "room": holder["room"], "surface": surface}
                else:
                    stage = doc.stage
                    status, body = 200, {"topic": stage.get("topic") or None,
                                         "ts": stage.get("ts", 0),
                                         "speaking": bool(stage.get("speaking")),
                                         "room": holder["room"]}
                    if surface != "html":
                        body["surface"] = surface
        except (RoomDocError, asyncio.TimeoutError, ConnectionError) as exc:
            status, body = 503, {"ok": False, "error": str(exc) or type(exc).__name__,
                                 "room": holder["room"]}
        payload = json.dumps(body).encode()
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                     + payload)
        try:
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port, limit=LINE_LIMIT)
    log(f"relay: http://{host}:{port} — POST /highlight/<topic>, /slide/<move>, /spot/<words>, "
        "/surface/html|html-<id>|board|doc|db, /page/<id>, /room/<id>, /db/<db>/row, /db/<db>/move, "
        "/db/<db>/row/<row>/body; GET /state, /outline, /rooms, /pages, /db, /db/<db>/row/<row>")
    async with server:
        await asyncio.gather(server.serve_forever(), keep_connected())
