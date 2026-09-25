"""A local HTTP relay onto an HTML page's shared stage.

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
  GET  /outline             slides, titles and pointable topics on the page (page_outline.py)
  POST /room/<room id>      hold that room instead (url-encoded `!abc:server`)
  GET  /rooms               the agent's joined rooms, {id, name}, to resolve a spoken name

It listens on 127.0.0.1 only: whoever reaches the port drives the stage as
this agent, so it must never be exposed beyond the machine.
"""
from __future__ import annotations

import asyncio
import json
import re

from room_collab_protocol import HTML_KIND, RoomDocError

TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ROOM_ID_RE = re.compile(r"![^\s:/]+:[^\s/]+")
SWITCH_WAIT_S = 8
RECONNECT_S = (1, 2, 5, 10, 30)


def visible_words(html: str) -> str:
    """The page's words, lower-cased and single-spaced, to tell a speaker whether a spotlight will land."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>", " ", html)
    import html as _html
    return " ".join(_html.unescape(text).lower().split())


def route(method: str, path: str) -> tuple[str, object] | tuple[int, dict]:
    """What a request asks for, as ("highlight", topic|None), ("speaking", bool),
    ("state", None), or an HTTP status and body to answer directly."""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    if method == "GET" and path == "/state":
        return ("state", None)
    if method == "GET" and path == "/script":
        return ("script", None)
    if method == "GET" and path == "/outline":
        return ("outline", None)
    if method == "GET" and path == "/rooms":
        return ("rooms", None)
    if method != "POST":
        return (405 if path.startswith(("/highlight/", "/slide/", "/spot/", "/speaking/", "/presenter/",
                                        "/room/"))
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
    if path in ("/presenter/on", "/presenter/off"):
        return (200, {"ok": True, "note": "presenting is controlled in the room"})
    return (404, {"ok": False, "error": "not found"})


async def read_script(open_text) -> dict:
    """The room Doc's talk script, parsed; opened per call since it is read once per talk."""
    from talk_script import extract, parse
    async with open_text() as doc:
        section = extract(doc.text)
    if section is None:
        raise RoomDocError('the room\'s Doc has no "Talk script" heading')
    return {"ok": True, "steps": parse(section)}


async def serve(open_doc, port: int, *, room: str, host: str = "127.0.0.1", log=print,
                open_text=None, list_rooms=None) -> None:
    """Hold `room`'s page open (reconnecting when it drops) and answer HTTP on `port`.
    `open_doc(room)` opens a room's page and `open_text(room)` its Doc, for GET /script;
    `list_rooms()` (blocking) returns the joined rooms as [{id, name}], for GET /rooms."""
    holder: dict = {"doc": None, "room": room, "error": None}
    ready = asyncio.Event()
    switched = asyncio.Event()

    async def hold_until_closed_or_switched(doc) -> None:
        waits = [asyncio.ensure_future(doc.closed()), asyncio.ensure_future(switched.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()

    async def keep_connected() -> None:
        attempt = 0
        while True:
            held = holder["room"]
            switched.clear()
            try:
                async with open_doc(held) as doc:
                    if doc.kind != HTML_KIND:
                        raise RoomDocError("the relay drives the HTML page: open it with --kind html")
                    if held == holder["room"]:
                        holder["doc"], holder["error"], attempt = doc, None, 0
                        ready.set()
                        log(f"relay: connected to {held}; {len(doc.text)} chars on the page")
                        await hold_until_closed_or_switched(doc)
            except RoomDocError as exc:
                holder["error"] = str(exc)
                log(f"relay: {exc}")
                if "--kind html" in str(exc):
                    raise
            holder["doc"] = None
            ready.clear()
            if holder["room"] != held:
                attempt = 0
                continue
            delay = RECONNECT_S[min(attempt, len(RECONNECT_S) - 1)]
            attempt += 1
            log(f"relay: reconnecting in {delay}s")
            try:
                await asyncio.wait_for(switched.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def switch_to(target: str) -> dict:
        if target != holder["room"]:
            log(f"relay: switching to {target}")
            holder.update(room=target, doc=None, error=None)
            ready.clear()
            switched.set()
        try:
            await asyncio.wait_for(ready.wait(), SWITCH_WAIT_S)
        except asyncio.TimeoutError:
            return {"ok": True, "room": target, "connected": False,
                    "error": holder["error"] or "not connected to the room yet; still trying"}
        if holder["doc"] is None or holder["room"] != target:
            return {"ok": False, "room": holder["room"], "error": "another switch overtook this one"}
        text = holder["doc"].text
        return {"ok": True, "room": target, "connected": True, "has_page": bool(text.strip()),
                "chars": len(text)}

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
            elif what[0] == "room":
                status, body = 200, await switch_to(what[1])
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
                doc = holder["doc"]
                if doc is None:
                    raise RoomDocError("the relay is switching rooms; try again")
                kind, arg = what
                if kind == "highlight":
                    state = await doc.set_stage(arg)
                    status, body = 200, {"ok": True, **state}
                elif kind == "spot":
                    spot = await doc.set_spot(arg)
                    found = arg is None or " ".join(arg.lower().split()) in visible_words(doc.text)
                    status, body = 200, {"ok": True, **spot, "found_on_page": found}
                elif kind == "outline":
                    from page_outline import outline
                    status, body = 200, {"ok": True, **outline(doc.text)}
                elif kind == "slide":
                    nav = await doc.navigate(*arg)
                    status, body = 200, {"ok": True, **nav}
                elif kind == "speaking":
                    await doc.set_speaking(bool(arg))
                    status, body = 200, {"ok": True, "speaking": bool(arg)}
                else:
                    stage = doc.stage
                    status, body = 200, {"topic": stage.get("topic") or None,
                                         "ts": stage.get("ts", 0),
                                         "speaking": bool(stage.get("speaking")),
                                         "room": holder["room"]}
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

    server = await asyncio.start_server(handle, host, port)
    log(f"relay: http://{host}:{port} — POST /highlight/<topic>, /room/<id>; GET /state, /rooms")
    async with server:
        await asyncio.gather(server.serve_forever(), keep_connected())
