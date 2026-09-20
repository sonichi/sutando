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
        Awareness, Doc, Map, Text, YMessageType, YSyncMessageType,
        create_awareness_message, create_sync_message, create_update_message,
        handle_sync_message, read_message,
    )
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        f"room-doc client needs its dependencies: {exc}\n"
        "install them with: pip install -r skills/room-doc/requirements.txt"
    ) from exc

from room_doc_board import (complete_element, # noqa: E402
    BOARD_KIND, ELEMENTS_KEY, FILES_KEY, changed_elements, describe_invalid,
    elements_from_map, is_board_element, is_board_file, live_elements,
)
from room_kanban import CARDS_KEY, KANBAN_KIND  # noqa: E402

from room_doc_protocol import (  # noqa: E402
    close_code,
    DEFAULT_KIND, DEFAULT_TEXT_NAME, RoomDocError, close_reason, doc_socket_url,
    explain,
)

# The server's attribution map. This client reads it and never writes it.
AUTHORS_KEY = "authors"
SYNC_TIMEOUT_S = 20.0
# Marks a transaction as ours, so the reconcile observer can ignore its own writes.
LOCAL_ORIGIN = "room-doc-client"


class RoomDoc:
    """One open document. Use `open_room_doc()` rather than constructing it."""

    def __init__(self, ws: Any, doc: Doc, awareness: Awareness, text_name: str,
                 kind: str = DEFAULT_KIND):
        self._ws = ws
        self._doc = doc
        self._awareness = awareness
        self._kind = kind
        self._text_name = text_name
        # ONLY the markdown document is a text; naming the non-text kinds
        # instead lets the next one read empty and accept unseen writes.
        self._text = doc.get(text_name, type=Text) if kind == DEFAULT_KIND else None
        # Two states, not one: a reader that dies is not a sync that finished.
        self._synced = asyncio.Event()
        self._ended: asyncio.Future = asyncio.get_event_loop().create_future()
        self._reader: asyncio.Task | None = None
        self._awareness_task: asyncio.Task | None = None
        self._awareness_sub: Any = None
        # What this session has claimed, so a concurrent merge that replaces one
        # of ours with an older version can be answered.
        self._asserted: dict[str, dict] = {}
        self._elements_sub: Any = None

    @property
    def kind(self) -> str:
        return self._kind

    def _require_text(self, what: str) -> Any:
        """A text operation on a structured document is refused, never answered
        with "". An empty string reads as an empty document, and a write that
        lands where nothing reads it is worse than an error."""
        if self._text is None:
            where = {BOARD_KIND: f"elements under {ELEMENTS_KEY!r}",
                     KANBAN_KIND: "cards and columns"}.get(
                         self._kind, "structured data")
            raise RoomDocError(
                f"cannot {what} on the {self._kind!r} document: it holds {where}, "
                f"not text. Only the {DEFAULT_KIND!r} document is a text — open "
                "that, or use the API for this kind.")
        return self._text

    @property
    def text(self) -> str:
        return str(self._require_text("read text"))

    def _require_board(self, what: str) -> tuple:
        if self._kind != BOARD_KIND:
            raise RoomDocError(
                f"cannot {what} on the {self._kind!r} document: elements live on the "
                f"board. Open it with kind={BOARD_KIND!r}.")
        return (self._doc.get(ELEMENTS_KEY, type=Map),
                self._doc.get(FILES_KEY, type=Map))

    @staticmethod
    def _items(ymap: Any) -> list:
        # A pycrdt Map is dict-like; snapshot it so callers cannot mutate the CRDT.
        return [(k, dict(v) if isinstance(v, dict) else v) for k, v in ymap.items()]

    @property
    def elements(self) -> list[dict]:
        """Every valid element, deleted ones included, in drawing order."""
        elements, _ = self._require_board("read elements")
        return elements_from_map(self._items(elements))

    @property
    def live_elements(self) -> list[dict]:
        """Only what is still on the canvas."""
        elements, _ = self._require_board("read elements")
        return live_elements(self._items(elements))

    @property
    def files(self) -> list[dict]:
        _, files = self._require_board("read files")
        return [v for k, v in self._items(files) if is_board_file(v, k)]

    @property
    def peers(self) -> list[dict]:
        out = []
        for state in self._awareness.states.values():
            if isinstance(state, dict) and isinstance(state.get("user"), dict):
                out.append(state["user"])
        return out

    @property
    def authors(self) -> dict[str, dict]:
        """Who wrote with each Yjs client id, as the SERVER recorded it.

        Read-only on purpose: this map is the server's, and a client that wrote
        to it would be claiming an identity rather than reporting one.
        """
        # No guard needed: resolving the key as a Map never raises, even when it
        # holds another type, and a non-dict value is filtered below.
        authors = self._doc.get(AUTHORS_KEY, type=Map)
        return {str(k): dict(v) for k, v in authors.items() if isinstance(v, dict)}

    def wrote(self, client_id: int | str) -> dict | None:
        """The author behind one client id, or None when unknown or disputed.

        A disputed id answers None rather than picking a claimant: two accounts
        used it, and guessing between them would invent the answer.
        """
        row = self.authors.get(str(client_id))
        if not row or "disputed" in row:
            return None
        return row

    def _require_live(self) -> None:
        if self._ended.done():
            raise RoomDocError(f"the document session has ended: {close_reason(self._ended.result())}")

    async def _send(self, payload: bytes) -> None:
        self._require_live()
        await self._ws.send(payload)

    async def _send_quietly(self, payload: bytes) -> None:
        """Presence renewal after the socket died is not an error worth a
        traceback per tick; the session's end is reported once, by _ended."""
        if self._ended.done():
            return
        try:
            await self._ws.send(payload)
        except Exception:  # noqa: BLE001
            pass

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
            # Nothing renews presence on a dead socket.
            if self._awareness_task:
                self._awareness_task.cancel()

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
            # Only our OWN state, changed locally. Re-sending a peer's update
            # makes this socket a holder of that peer's id, so it never expires.
            if kind != "update" or changes[1] != "local":
                return
            mine = self._awareness.client_id
            if mine not in [i for group in changes[0].values() for i in group]:
                return
            update = self._awareness.encode_awareness_update([mine])
            asyncio.ensure_future(self._send_quietly(create_awareness_message(update)))

        self._awareness_sub = self._awareness.observe(on_change)
        self._awareness_task = asyncio.create_task(self._awareness.start())

    async def _stop(self) -> None:
        if self._elements_sub is not None:
            try:
                self._doc.get(ELEMENTS_KEY, type=Map).unobserve(self._elements_sub)
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                pass
            self._elements_sub = None
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
        with self._doc.transaction(origin=LOCAL_ORIGIN):
            mutate()
        await self._send(create_update_message(self._doc.get_update(before)))

    async def append(self, addition: str) -> None:
        text = self._require_text("append text")
        await self._commit(lambda: text.__iadd__(addition))

    async def insert(self, index: int, addition: str) -> None:
        text = self._require_text("insert text")
        await self._commit(lambda: text.insert(index, addition))

    async def put_elements(self, elements: list[dict]) -> int:
        """Write elements that are newer than what is stored. Returns how many.

        Refuses an invalid element rather than writing it: the web client
        validates on read, so a bad one would be dropped by every viewer with
        no error anywhere.
        """
        ymap, _ = self._require_board("write elements")
        for element in elements:
            if not is_board_element(element):
                raise RoomDocError(
                    f"not a board element: {describe_invalid(element)}. "
                    "Nothing was written.")
        # Filled here, at the one writer: the panel hands the map to the editor
        # as-is, and a minimal element throws inside its selection handler.
        stored = dict(self._items(ymap))
        elements = [complete_element(e, base=stored.get(e.get("id"))) for e in elements]
        changed = changed_elements(elements, stored.get)
        # Remembered even when nothing is written: a concurrent merge can still
        # replace a value we already agreed with, and then it needs re-asserting.
        for element in elements:
            self._asserted[element["id"]] = dict(element)
        self._watch_for_regression()
        if not changed:
            return 0

        def mutate() -> None:
            for element in changed:
                ymap[element["id"]] = dict(element)

        await self._commit(mutate)
        return len(changed)

    async def delete_element(self, element_id: str) -> None:
        """Mark an element deleted — a newer write, not a removal, because that
        is how the editor reconciles a deletion."""
        ymap, _ = self._require_board("delete an element")
        stored = dict(self._items(ymap)).get(element_id)
        if not is_board_element(stored, element_id):
            raise RoomDocError(f"no such element on the board: {element_id!r}")
        gone = dict(stored)
        gone["isDeleted"] = True
        gone["version"] = int(stored.get("version", 0)) + 1
        await self.put_elements([gone])

    def _watch_for_regression(self) -> None:
        """Re-assert our elements whenever a REMOTE change lands on one.

        Concurrent writes to one key are merged by Yjs on client id, which knows
        nothing of element versions, so the older version can win. Re-applying
        makes ours causally later and it wins everywhere; when theirs is
        genuinely newer this writes nothing.
        """
        if self._elements_sub is not None or self._kind != BOARD_KIND:
            return
        ymap, _ = self._require_board("watch elements")

        def on_map(event: Any) -> None:
            origin = getattr(getattr(event, "transaction", None), "origin", None)
            if origin == LOCAL_ORIGIN:
                return
            touched = set(getattr(event, "keys", None) or {})
            mine = [e for i, e in self._asserted.items() if i in touched]
            if mine:
                asyncio.ensure_future(self._reassert(mine))

        self._elements_sub = ymap.observe(on_map)

    async def _reassert(self, elements: list[dict]) -> None:
        # A background re-assert must never raise into the event loop: the
        # session can end between the remote change and this running.
        try:
            await self.put_elements(elements)
        except RoomDocError:
            pass

    async def changes(self, settle: float = 0.0) -> AsyncIterator[str]:
        """Every remote edit to the text, yielded as the text after it landed.

        The connection is held for as long as the caller iterates. Local
        writes are not reported: the caller made them. Ends when the session
        does, by raising the close reason rather than stopping quietly — a
        watcher that exits silently looks exactly like one that saw nothing.

        `settle` > 0 coalesces: the server forwards one push per keystroke
        (measured), so a person typing "@mars please" is a dozen pushes. With
        settle the text is yielded once, `settle` seconds after the last one.
        """
        text = self._require_text("watch text")
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def on_text(event: Any) -> None:
            origin = getattr(getattr(event, "transaction", None), "origin", None)
            if origin != LOCAL_ORIGIN:
                queue.put_nowait(str(text))

        sub = text.observe(on_text)
        # shield: cancelling this waiter must not cancel the session's own future.
        ended = asyncio.ensure_future(asyncio.shield(self._ended))
        pending: str | None = None
        try:
            while True:
                got = asyncio.ensure_future(queue.get())
                timeout = settle if (settle > 0 and pending is not None) else None
                done, _ = await asyncio.wait({got, ended}, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if ended in done:
                    got.cancel()
                    raise RoomDocError(
                        f"the document session has ended: {close_reason(ended.result())}")
                if got in done:
                    if settle > 0:
                        pending = got.result()      # keep the newest; wait for quiet
                        continue
                    yield got.result()
                    continue
                got.cancel()                        # quiet for `settle`: emit once
                yield pending
                pending = None
        finally:
            text.unobserve(sub)
            if not ended.done():
                ended.cancel()

    def snapshot(self) -> dict:
        """What this document looks like right now, for whichever kind it is,
        plus who is present — the unit `events()` diffs."""
        snap: dict = {"peers": list(self.peers)}
        if self._kind == DEFAULT_KIND:
            snap["text"] = self.text
        elif self._kind == BOARD_KIND:
            snap["elements"] = self.elements
        elif self._kind == KANBAN_KIND:
            snap["cards"] = {k: v for k, v in self._items(self._doc.get(CARDS_KEY, type=Map))
                             if isinstance(v, dict)}
        return snap

    async def events(self, handles: list[str], settle: float = 1.0,
                     since: dict | None = None) -> AsyncIterator[dict]:
        """What happened that concerns `handles`, as it happens: a text or board
        line that @-mentions one, a kanban card assigned to or moved for one,
        a peer arriving or leaving. One stream for every kind, so an agent
        holds one connection and one loop.

        Snapshots are taken after `settle` seconds of quiet (a keystroke is a
        push), diffed against the last one emitted, and the differences that
        concern the handles are yielded. Ends by raising the close reason,
        with `.code` set so a caller can tell a restart from a refusal.

        `since` is a snapshot from an earlier session: on reconnect, what
        landed while the socket was down is diffed too, not silently skipped.
        """
        from room_doc_watch import (addressed_to, board_mentions, kanban_changes,
                                    new_lines, peer_changes)
        queue: asyncio.Queue[None] = asyncio.Queue()

        def poke(*_: Any) -> None:
            queue.put_nowait(None)

        def on_doc(event: Any) -> None:
            origin = getattr(getattr(event, "transaction", None), "origin", None)
            if origin != LOCAL_ORIGIN:
                poke()

        subs = []
        if self._kind == DEFAULT_KIND:
            subs.append((self._text, self._text.observe(on_doc)))
        elif self._kind == BOARD_KIND:
            m = self._doc.get(ELEMENTS_KEY, type=Map)
            subs.append((m, m.observe(on_doc)))
        elif self._kind == KANBAN_KIND:
            m = self._doc.get(CARDS_KEY, type=Map)
            subs.append((m, m.observe(on_doc)))
        aw_sub = self._awareness.observe(lambda *_: poke())
        ended = asyncio.ensure_future(asyncio.shield(self._ended))
        last = since if since is not None else self.snapshot()
        # A carried snapshot is compared at once: the gap may hold a mention.
        dirty = since is not None
        try:
            while True:
                got = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({got, ended}, timeout=settle if dirty else None,
                                             return_when=asyncio.FIRST_COMPLETED)
                if ended in done:
                    got.cancel()
                    err = RoomDocError(
                        f"the document session has ended: {close_reason(ended.result())}")
                    err.code = close_code(ended.result())
                    err.snapshot = last
                    raise err
                if got in done:
                    dirty = True
                    continue
                got.cancel()
                dirty = False
                now = self.snapshot()
                out: list[dict] = []
                if "text" in now:
                    for line in addressed_to(new_lines(last["text"], now["text"]), handles):
                        out.append({"kind": "mention", "where": "text", "text": line})
                if "elements" in now:
                    out += board_mentions(last["elements"], now["elements"], handles)
                if "cards" in now:
                    out += kanban_changes(last["cards"], now["cards"], handles)
                out += peer_changes(last["peers"], now["peers"])
                last = now
                for ev in out:
                    yield ev
        finally:
            for obj, sub in subs:
                obj.unobserve(sub)
            self._awareness.unobserve(aw_sub)
            if not ended.done():
                ended.cancel()

    async def reconcile(self, elements: list[dict] | None = None) -> int:
        """Re-assert elements now. Rarely needed by hand — `put_elements` arms
        an observer that does this on every remote change."""
        return await self.put_elements(
            elements if elements is not None else list(self._asserted.values()))

    async def replace(self, old: str, new: str) -> None:
        """Replace the first occurrence. Refuses when absent, so a caller never
        silently writes nothing."""
        self._require_text("replace text")
        current = self.text
        at = current.find(old)
        if at < 0:
            raise RoomDocError(f"text to replace is not in the document: {old[:60]!r}")

        text = self._require_text("replace text")
        # pycrdt indexes Text by UTF-8 BYTES; str.find counts characters. Every
        # multi-byte character before the match would otherwise shift the write.
        start = len(current[:at].encode("utf-8"))
        width = len(old.encode("utf-8"))

        def mutate() -> None:
            del text[start:start + width]
            text.insert(start, new)

        await self._commit(mutate)

    async def settle(self, seconds: float = 1.0) -> None:
        """Wait for the server to acknowledge, and fail if it refused instead."""
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

    room_doc = RoomDoc(ws, doc, awareness, text_name, kind=kind)
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
