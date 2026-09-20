"""A y-websocket client for a room's live document, for agents.

A document is a CRDT, not a file: two writers merge character by character, so
an agent appends its paragraph while a human types in the same line and neither
loses work. That is why this is a live connection and not a PUT.

The framing comes from pycrdt itself — the same implementation the service
runs — rather than being hand-rolled here, so the two ends cannot drift.

Presence is a second channel (awareness) that never enters document history.
It EXPIRES: the server drops a peer that stops renewing, so holding the socket
open is not enough — the renewal loop runs for as long as the session does.
"""
from __future__ import annotations

import asyncio
import os
import ssl
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import websockets
    from pycrdt import (
        Awareness, Doc, Text, YMessageType, YSyncMessageType,
        create_awareness_message, create_sync_message, create_update_message,
        handle_sync_message, read_message,
    )
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        f"room-doc client needs its dependencies: {exc}\n"
        "install them with: pip install -r skills/room-doc/requirements.txt"
    ) from exc

from room_doc_protocol import (  # noqa: E402
    DEFAULT_KIND, DEFAULT_TEXT_NAME, RoomDocError, close_reason, doc_socket_url,
    explain,
)

SYNC_TIMEOUT_S = 20.0


class RoomDoc:
    """One open document. Use `open_room_doc()` rather than constructing it."""

    def __init__(self, ws: Any, doc: Doc, awareness: Awareness, text_name: str):
        self._ws = ws
        self._doc = doc
        self._awareness = awareness
        self._text = doc.get(text_name, type=Text)
        # Two states, not one: a reader that dies is not a sync that finished.
        self._synced = asyncio.Event()
        self._ended: asyncio.Future = asyncio.get_event_loop().create_future()
        self._reader: asyncio.Task | None = None
        self._awareness_task: asyncio.Task | None = None
        self._awareness_sub: Any = None

    @property
    def text(self) -> str:
        return str(self._text)

    @property
    def peers(self) -> list[dict]:
        out = []
        for state in self._awareness.states.values():
            if isinstance(state, dict) and isinstance(state.get("user"), dict):
                out.append(state["user"])
        return out

    def _require_live(self) -> None:
        if self._ended.done():
            raise RoomDocError(f"the document session has ended: {close_reason(self._ended.result())}")

    async def _send(self, payload: bytes) -> None:
        self._require_live()
        await self._ws.send(payload)

    async def _handle(self, data: bytes) -> None:
        if data[0] == YMessageType.SYNC:
            reply = handle_sync_message(data[1:], self._doc)
            if data[1] == YSyncMessageType.SYNC_STEP2:
                self._synced.set()
            if reply is not None:
                await self._ws.send(reply)
        elif data[0] == YMessageType.AWARENESS:
            self._awareness.apply_awareness_update(read_message(data[1:]), "remote")

    async def _read_loop(self) -> None:
        ended = None
        try:
            async for raw in self._ws:
                await self._handle(raw.encode() if isinstance(raw, str) else raw)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            ended = exc
        finally:
            if not self._ended.done():
                self._ended.set_result(ended)

    async def _start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await self._ws.send(create_sync_message(self._doc))
        except Exception as exc:  # noqa: BLE001 - a refusal arrives as a close
            raise RoomDocError(f"the document did not open: {close_reason(exc)}") from exc
        waiter = asyncio.ensure_future(self._synced.wait())
        try:
            await asyncio.wait({waiter, self._ended}, timeout=SYNC_TIMEOUT_S,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if self._synced.is_set():
            self._start_presence_renewal()
            return
        if self._ended.done():
            # The service accepts and THEN closes for a bad room or kind, so a
            # refusal arrives here rather than at the handshake.
            raise RoomDocError(f"the document did not open: {close_reason(self._ended.result())}")
        raise RoomDocError(f"connected but the document never synced within {SYNC_TIMEOUT_S:g}s")

    def _start_presence_renewal(self) -> None:
        """Forward local awareness changes, and keep the renewal loop running.

        Without it the server expires this peer and the people sharing the
        document stop seeing it, while the socket and the editing carry on.
        """
        def on_change(kind: str, changes: tuple) -> None:
            # Only our own state, changed locally: re-sending a remote update
            # makes this socket a holder of that peer's id, so it never leaves.
            if kind != "update" or changes[1] != "local":
                return
            mine = self._awareness.client_id
            if mine not in [i for group in changes[0].values() for i in group]:
                return
            update = self._awareness.encode_awareness_update([mine])
            asyncio.ensure_future(self._ws.send(create_awareness_message(update)))

        self._awareness_sub = self._awareness.observe(on_change)
        self._awareness_task = asyncio.create_task(self._awareness.start())

    async def _stop(self) -> None:
        if self._awareness_sub is not None:
            self._awareness.unobserve(self._awareness_sub)
            self._awareness_sub = None
        try:
            await self._awareness.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            pass
        if self._awareness_task:
            self._awareness_task.cancel()
            try:
                await self._awareness_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass

    async def set_presence(self, name: str, color: str = "#f26d5b", kind: str = "agent",
                           user_id: str | None = None) -> None:
        """Publish who is here. Without it the agent edits invisibly.

        `user_id` is what the roster looks an avatar up by; a name cannot be
        resolved to one. Omit it and the agent shows a name and no face, which
        is deliberate — a wrong face is worse than none.
        """
        state = {"name": name, "color": color, "colorLight": f"{color}33", "kind": kind}
        if user_id:
            state["userId"] = user_id
        self._awareness.set_local_state_field("user", state)
        await self._send(create_awareness_message(
            self._awareness.encode_awareness_update([self._awareness.client_id])))

    async def _commit(self, mutate: Callable[[], None]) -> None:
        """Apply a local change and put only the delta on the wire."""
        self._require_live()
        before = self._doc.get_state()
        mutate()
        await self._send(create_update_message(self._doc.get_update(before)))

    async def append(self, addition: str) -> None:
        await self._commit(lambda: self._text.__iadd__(addition))

    async def insert(self, index: int, addition: str) -> None:
        await self._commit(lambda: self._text.insert(index, addition))

    async def replace(self, old: str, new: str) -> None:
        """Replace the first occurrence. Refuses when absent, so a caller never
        silently writes nothing."""
        at = self.text.find(old)
        if at < 0:
            raise RoomDocError(f"text to replace is not in the document: {old[:60]!r}")

        def mutate() -> None:
            del self._text[at:at + len(old)]
            self._text.insert(at, new)

        await self._commit(mutate)

    async def settle(self, seconds: float = 1.0) -> None:
        """A grace period before closing, then a check that the session lived.

        Not an acknowledgement: nothing here waits for the server to confirm
        the update. Naming it so would promise more than it does.
        """
        await asyncio.sleep(seconds)
        self._require_live()


@asynccontextmanager
async def open_room_doc(api_root: str, room_id: str, token: str, *,
                        kind: str = DEFAULT_KIND,
                        text_name: str = DEFAULT_TEXT_NAME,
                        insecure: bool = False) -> AsyncIterator[RoomDoc]:
    """Open one of a room's documents. `kind` selects which — the default
    markdown document, or a separate one such as the board."""
    url = doc_socket_url(api_root, room_id, kind=kind)
    sslctx = None
    if url.startswith("wss://"):
        sslctx = ssl.create_default_context()
        if insecure:
            # Only for a local rig with a self-signed certificate.
            sslctx.check_hostname = False
            sslctx.verify_mode = ssl.CERT_NONE

    doc = Doc()
    awareness = Awareness(doc)
    try:
        connection = websockets.connect(url, subprotocols=["bearer", token], ssl=sslctx,
                                        max_size=64 * 1024 * 1024)
        ws = await connection.__aenter__()
    except RoomDocError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RoomDocError(explain(exc, url)) from exc

    room_doc = RoomDoc(ws, doc, awareness, text_name)
    try:
        await room_doc._start()
    except BaseException:
        await connection.__aexit__(*sys.exc_info())
        raise
    try:
        yield room_doc
    finally:
        await room_doc._stop()
        await connection.__aexit__(None, None, None)
