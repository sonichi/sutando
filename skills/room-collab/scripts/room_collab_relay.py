"""A local HTTP relay onto an HTML page's shared stage.

A voice agent's tool must answer within a turn; opening the room per call costs
seconds on a large page. The relay holds one connection and speaks the local
talk-highlight API, so an existing voice tool drives the room's page unchanged:

  POST /highlight/<topic>   (topic `clear` clears)
  POST /slide/next|prev|<n> move every viewer's deck
  POST /speaking/on|off
  POST /presenter/on|off    accepted; presenting is the room's, not the relay's
  GET  /state               {topic, ts, speaking}

It listens on 127.0.0.1 only: whoever reaches the port drives the stage as
this agent, so it must never be exposed beyond the machine.
"""
from __future__ import annotations

import asyncio
import json
import re

from room_collab_protocol import HTML_KIND, RoomDocError

TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECONNECT_S = (1, 2, 5, 10, 30)


def route(method: str, path: str) -> tuple[str, object] | tuple[int, dict]:
    """What a request asks for, as ("highlight", topic|None), ("speaking", bool),
    ("state", None), or an HTTP status and body to answer directly."""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    if method == "GET" and path == "/state":
        return ("state", None)
    if method != "POST":
        return (405 if path.startswith(("/highlight/", "/slide/", "/speaking/", "/presenter/")) else 404,
                {"ok": False, "error": "not found"})
    if path.startswith("/highlight/"):
        topic = path[len("/highlight/"):].lower()
        if topic == "clear":
            return ("highlight", None)
        if not TOPIC_RE.fullmatch(topic):
            return (400, {"ok": False, "error": f"not a topic key: {topic!r}"})
        return ("highlight", topic)
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


async def serve(open_doc, port: int, *, host: str = "127.0.0.1", log=print) -> None:
    """Hold the page open (reconnecting when it drops) and answer HTTP on `port`."""
    holder: dict = {"doc": None}
    ready = asyncio.Event()

    async def keep_connected() -> None:
        attempt = 0
        while True:
            try:
                async with open_doc() as doc:
                    if doc.kind != HTML_KIND:
                        raise RoomDocError("the relay drives the HTML page: open it with --kind html")
                    holder["doc"], attempt = doc, 0
                    ready.set()
                    log(f"relay: connected; {len(doc.text)} chars on the page")
                    await doc.closed()
            except RoomDocError as exc:
                log(f"relay: {exc}")
                if "--kind html" in str(exc):
                    raise
            holder["doc"] = None
            ready.clear()
            delay = RECONNECT_S[min(attempt, len(RECONNECT_S) - 1)]
            attempt += 1
            log(f"relay: reconnecting in {delay}s")
            await asyncio.sleep(delay)

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
            else:
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                except asyncio.TimeoutError:
                    raise RoomDocError("not connected to the room yet") from None
                doc = holder["doc"]
                kind, arg = what
                if kind == "highlight":
                    state = await doc.set_stage(arg)
                    status, body = 200, {"ok": True, **state}
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
                                         "speaking": bool(stage.get("speaking"))}
        except (RoomDocError, asyncio.TimeoutError, ConnectionError) as exc:
            status, body = 503, {"ok": False, "error": str(exc) or type(exc).__name__}
        payload = json.dumps(body).encode()
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                     + payload)
        try:
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    log(f"relay: http://{host}:{port} — POST /highlight/<topic>, /speaking/on|off; GET /state")
    async with server:
        await asyncio.gather(server.serve_forever(), keep_connected())
