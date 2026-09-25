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
import base64
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
    from room_collab_positions import (as_awareness_json, encode as encode_position,
                                       relative_position, units)
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        f"room-collab client needs its dependencies: {exc}\n"
        "  pip install -r skills/room-collab/requirements.txt\n"
        "    works in a virtualenv, or on any python whose pip may install.\n"
        "  python3 -m venv DIR && DIR/bin/pip install -r skills/room-collab/requirements.txt\n"
        "    then run this script with DIR/bin/python3. Needed on a managed\n"
        "    python (Homebrew, Debian), where the line above refuses with\n"
        "    error: externally-managed-environment."
    ) from exc

from room_collab_board import (complete_element, # noqa: E402
    BOARD_KIND, ELEMENTS_KEY, FILES_KEY, changed_elements, describe_invalid,
    elements_from_map, is_board_element, is_board_file, live_elements,
)
from room_kanban import (  # noqa: E402
    CARDS_KEY, COLUMNS_KEY, KANBAN_KIND, changed as changed_cards, describe_invalid as describe_bad_card,
    is_card, is_column, normalized,
)

from room_composer import (  # noqa: E402
    COMPOSER_KIND, POSTS_KEY, ComposerError, build as build_post, feed as post_feed,
    mark as status_mark, root_for,
)

from room_collab_protocol import (  # noqa: E402
    close_code,
    DEFAULT_KIND, DEFAULT_TEXT_NAME, TEXT_ROOTS, RoomDocError, close_reason, doc_socket_url,
    explain,
    http_status,
    unanswered,
)

# The server's attribution map. This client reads it and never writes it.
AUTHORS_KEY = "authors"
SYNC_TIMEOUT_S = 20.0
# Marks a transaction as ours, so the reconcile observer can ignore its own writes.
LOCAL_ORIGIN = "room-collab-client"
# pycrdt stamps an awareness change this process made with this origin.
LOCAL_AWARENESS_ORIGIN = "local"


class RoomDoc:
    """One open document. Use `open_room_collab()` rather than constructing it."""

    def __init__(self, ws: Any, doc: Doc, awareness: Awareness, text_name: str,
                 kind: str = DEFAULT_KIND):
        self._ws = ws
        self._doc = doc
        self._awareness = awareness
        self._kind = kind
        self._text_name = text_name
        # Only the text kinds have a text by default; a composer holds one per
        # post, which `open_post` selects. Naming another kind accepts unseen writes.
        self._text = doc.get(text_name, type=Text) if kind in TEXT_ROOTS else None
        self._post_id: str | None = None
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
            if self._kind == COMPOSER_KIND:
                raise RoomDocError(
                    f"cannot {what} on the {COMPOSER_KIND!r} document until a post is open: it "
                    "holds one text per post, so a write with no post named has no destination. "
                    "Call open_post(<id>) or add_post(...) first.")
            where = {BOARD_KIND: f"elements under {ELEMENTS_KEY!r}",
                     KANBAN_KIND: "cards and columns"}.get(
                         self._kind, "structured data")
            raise RoomDocError(
                f"cannot {what} on the {self._kind!r} document: it holds {where}, "
                f"not text. Only the {', '.join(map(repr, TEXT_ROOTS))} documents are text — "
                "open one of those, or use the API for this kind.")
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

    def anchor(self, start: int, end: int) -> dict[str, str]:
        """Two Yjs relative positions, base64, for the character range
        [start, end) of the text — the form a web client anchors a comment to,
        which survives edits elsewhere in the document."""
        text = self._require_text("anchor text")
        current = self.text
        if not 0 <= start <= end <= len(current):
            raise RoomDocError(f"anchor range {start}:{end} is outside the text ({len(current)} chars)")
        # Character offsets here; a Yjs position counts UTF-16 units.
        return {"start": base64.b64encode(
                    encode_position(self._doc, text, self._text_name, units(current[:start]))).decode("ascii"),
                "end": base64.b64encode(
                    encode_position(self._doc, text, self._text_name, units(current[:end]))).decode("ascii")}

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
            raise RoomDocError(f"the surface session has ended: {close_reason(self._ended.result())}")

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
            raise RoomDocError(f"the surface did not open: {close_reason(exc)}") from exc
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
            raise RoomDocError(f"the surface did not open: {close_reason(self._ended.result())}")
        raise RoomDocError(f"connected but the surface never synced within {SYNC_TIMEOUT_S:g}s")

    def _start_presence_renewal(self) -> None:
        """Forward local awareness changes, and keep the renewal loop running.

        Without it the server expires this peer and the people sharing the
        document stop seeing it, while the socket and the editing carry on.
        """
        def on_change(kind: str, changes: tuple) -> None:
            # Only our OWN state, changed locally. Re-sending a peer's update
            # makes this socket a holder of that peer's id, so it never expires.
            if kind != "update" or changes[1] != LOCAL_AWARENESS_ORIGIN:
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
        if self._text is not None:
            # A person's editor shows a caret for as long as it is open; so does
            # this one — at the end of the text until a write moves it.
            await self._publish_cursor(len(str(self._text).encode("utf-8")))

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
        await self._publish_cursor(len(str(text).encode("utf-8")))

    async def insert(self, index: int, addition: str) -> None:
        text = self._require_text("insert text")
        await self._commit(lambda: text.insert(index, addition))
        await self._publish_cursor(index + len(addition.encode("utf-8")))

    async def _publish_cursor(self, index: int) -> None:
        """Put the agent's caret at `index` (the store's units) for the editors
        to draw: the same awareness `cursor` a person's editor publishes — a
        relative position, so it follows the text as others type around it."""
        if self._text is None:
            return
        try:
            # The writes count UTF-8 bytes; a Yjs position counts UTF-16 units.
            at = units(str(self._text).encode("utf-8")[:index].decode("utf-8"))
            pos = as_awareness_json(
                relative_position(self._doc, self._text, self._text_name, at), self._text_name)
        except BaseException:  # noqa: BLE001 - a pyo3 panic, or a cancel mid-send; the write already landed
            return  # deliberately wider than Exception: a panic is not one
        self._awareness.set_local_state_field("cursor", {"anchor": pos, "head": pos})
        await self._send_quietly(create_awareness_message(
            self._awareness.encode_awareness_update([self._awareness.client_id])))

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
                        f"the surface session has ended: {close_reason(ended.result())}")
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
        if self._kind in TEXT_ROOTS:
            snap["text"] = self.text
        elif self._kind == BOARD_KIND:
            snap["elements"] = self.elements
        elif self._kind == KANBAN_KIND:
            snap["cards"] = {k: v for k, v in self._items(self._doc.get(CARDS_KEY, type=Map))
                             if isinstance(v, dict)}
        return snap

    def _observe_changes(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Subscribe `fn` to every change in this surface — any remote document
        edit, and any awareness change that carries new peer state — and return
        an unsubscribe.

        One switch over the document kinds, shared by `events` and
        `on_activity`: two copies drift the moment a fourth kind lands and only
        one of them is taught about it.

        Awareness is filtered to remote `change` events. `Awareness.start()`
        re-sends this client's own state every `outdated_timeout/2` to stop the
        server expiring it, and each of those emits a local `update`: counting
        them made an idle timer read a silent surface as busy forever, so the
        30-minute drop could never fire. A remote renewal is an `update` too and
        carries no new state, so only `change` is activity.
        """
        subs = []

        def on_doc(event: Any) -> None:
            origin = getattr(getattr(event, "transaction", None), "origin", None)
            if origin != LOCAL_ORIGIN:
                fn()

        if self._kind in TEXT_ROOTS:
            subs.append((self._text, self._text.observe(on_doc)))
        elif self._kind == BOARD_KIND:
            m = self._doc.get(ELEMENTS_KEY, type=Map)
            subs.append((m, m.observe(on_doc)))
        elif self._kind == KANBAN_KIND:
            m = self._doc.get(CARDS_KEY, type=Map)
            subs.append((m, m.observe(on_doc)))
        def on_awareness(kind: str, changes: tuple) -> None:
            if kind == "change" and (len(changes) < 2 or changes[1] != LOCAL_AWARENESS_ORIGIN):
                fn()

        aw_sub = self._awareness.observe(on_awareness)

        def stop() -> None:
            for obj, sub in subs:
                obj.unobserve(sub)
            self._awareness.unobserve(aw_sub)

        return stop

    async def closed(self) -> "RoomDocError":
        """Resolve when this surface's session ends, with the reason.

        A holder that only sleeps never learns the socket died: `_read_loop`
        sets the end, it does not raise into the caller.
        """
        ended = await asyncio.shield(self._ended)
        err = RoomDocError(f"the surface session has ended: {close_reason(ended)}")
        err.code = close_code(ended)
        return err

    def on_activity(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback()` whenever ANYTHING changes in this surface, not only
        what concerns a handle.

        `events` answers "what here is addressed to me"; an idle timer on
        presence needs the other question — someone else's keystroke is exactly
        the moment an agent's presence is worth showing, so it must count.
        """
        return self._observe_changes(callback)

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
        from room_collab_watch import (addressed_to, board_mentions, kanban_changes,
                                    new_lines, peer_changes)
        queue: asyncio.Queue[None] = asyncio.Queue()

        def poke(*_: Any) -> None:
            queue.put_nowait(None)

        stop_observing = self._observe_changes(poke)
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
                        f"the surface session has ended: {close_reason(ended.result())}")
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
            stop_observing()
            if not ended.done():
                ended.cancel()

    # --- the kanban: cards and columns, the panel's rules

    def _require_composer(self, what: str) -> Any:
        if self._kind != COMPOSER_KIND:
            raise RoomDocError(
                f"cannot {what} on the {self._kind!r} document: posts live on the composer. "
                f"Open it with kind={COMPOSER_KIND!r}.")
        return self._doc.get(POSTS_KEY, type=Map)

    @staticmethod
    def _rules(fn: Callable[[], Any]) -> Any:
        # A rule refusal reaches the caller as the one error this client raises;
        # `room_composer` imports nothing, so it cannot raise that error itself.
        try:
            return fn()
        except ComposerError as exc:
            raise RoomDocError(str(exc)) from exc

    @property
    def post(self) -> str | None:
        """The post every text operation currently addresses, if one is open."""
        return self._post_id

    def posts(self) -> list[dict]:
        """Every post on the surface, oldest first, each with its text root."""
        return post_feed(self._require_composer("list posts").to_py() or {})

    def open_post(self, post_id: str) -> str:
        """Point the text operations at one post, and the caret with them.

        The caret carries the root's name, so switching posts is what tells the
        editors which one this agent is writing in — no extra message.
        """
        posts = self._require_composer("open a post")
        name = self._rules(lambda: root_for(post_id))
        if str(post_id) not in {str(k) for k, _ in self._items(posts)}:
            raise RoomDocError(
                f"no post {post_id!r} on this composer. A text root with no row beside it is "
                "invisible: the feed is built from the rows. Add it with add_post first.")
        self._text = self._doc.get(name, type=Text)
        self._text_name = name
        self._post_id = str(post_id)
        return name

    async def add_post(self, post_id: str, artifact_type: str, fields: dict | None = None,
                       created: int | None = None) -> dict:
        """File one post and open it. The row is refused here if a renderer
        could not draw it, rather than stored for the client to discover."""
        posts = self._require_composer("add a post")
        name = self._rules(lambda: root_for(post_id))
        row = self._rules(lambda: build_post(artifact_type, fields, created))
        if str(post_id) in {str(k) for k, _ in self._items(posts)}:
            raise RoomDocError(
                f"post {post_id!r} is already on this composer. Writing it again would replace "
                "the row someone else may be editing; open it instead.")
        # One key per post: a write to the whole map would drop a post filed
        # concurrently, which is the failure this surface exists to prevent.
        await self._commit(lambda: posts.__setitem__(str(post_id), row))
        self._text = self._doc.get(name, type=Text)
        self._text_name = name
        self._post_id = str(post_id)
        return row

    async def set_status(self, post_id: str, status: str, at: int | None = None) -> dict:
        """Move a post along its life: draft, sent, dropped.

        The row is rewritten whole, so two clients changing the SAME post's
        fields at once keep only one — which is why the prose is not in here.
        """
        posts = self._require_composer("set a post's status")
        current = dict(self._items(posts)).get(str(post_id))
        if not isinstance(current, dict):
            raise RoomDocError(
                f"no post {post_id!r} on this composer to mark {status!r}.")
        row = {**current, **self._rules(lambda: status_mark(status, at))}
        await self._commit(lambda: posts.__setitem__(str(post_id), row))
        return row

    def _require_kanban(self, what: str) -> tuple:
        if self._kind != KANBAN_KIND:
            raise RoomDocError(
                f"cannot {what} on the {self._kind!r} document: cards live on the "
                f"kanban. Open it with kind={KANBAN_KIND!r}.")
        return (self._doc.get(CARDS_KEY, type=Map), self._doc.get(COLUMNS_KEY, type=Map))

    @property
    def cards(self) -> list[tuple[str, dict]]:
        """Every well-formed card, tombstones included, as (id, card)."""
        cards, _ = self._require_kanban("read cards")
        return [(k, v) for k, v in self._items(cards) if is_card(v, k)]

    @property
    def columns(self) -> list[dict]:
        """The columns in board order."""
        _, columns = self._require_kanban("read columns")
        rows = [v for k, v in self._items(columns) if is_column(v, k)]
        return sorted(rows, key=lambda c: (c["order"], c["id"]))

    async def put_cards(self, cards: list[dict]) -> int:
        """Write cards that are newer than what is stored. Returns how many.
        Refuses a card the panel would drop, rather than writing it."""
        ymap, _ = self._require_kanban("write cards")
        cards = [normalized(c) if isinstance(c, dict) else c for c in cards]
        for card in cards:
            if not is_card(card):
                raise RoomDocError(f"not a kanban card: {describe_bad_card(card)}. Nothing was written.")
        stored = dict(self._items(ymap))
        todo = changed_cards(cards, stored.get)
        if not todo:
            return 0

        def mutate() -> None:
            for card in todo:
                ymap[card["id"]] = dict(card)

        await self._commit(mutate)
        return len(todo)

    async def put_columns(self, columns: list[dict]) -> int:
        _, ymap = self._require_kanban("write columns")
        for col in columns:
            if not is_column(col):
                raise RoomDocError(f"not a kanban column: {col!r}. Nothing was written.")
        stored = dict(self._items(ymap))
        todo = changed_cards(columns, stored.get, valid=is_column)
        if not todo:
            return 0

        def mutate() -> None:
            for col in todo:
                ymap[col["id"]] = dict(col)

        await self._commit(mutate)
        return len(todo)

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
        await self._publish_cursor(start + len(new.encode("utf-8")))

    async def settle(self, seconds: float = 1.0) -> None:
        """Wait for the server to acknowledge, and fail if it refused instead."""
        await asyncio.sleep(seconds)
        self._require_live()


@asynccontextmanager
async def open_room_collab(api_root: str, room_id: str, token: str, *,
                        kind: str = DEFAULT_KIND,
                        text_name: str | None = None,
                        insecure: bool = False) -> AsyncIterator[RoomDoc]:
    """Open one of a room's surfaces. `kind` selects which — the default
    markdown document, the HTML page, or a structured surface such as the board
    or the kanban. A text kind's root comes from TEXT_ROOTS unless named."""
    text_name = text_name or TEXT_ROOTS.get(kind, DEFAULT_TEXT_NAME)
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
        err = RoomDocError(explain(exc, url))
        err.status = http_status(exc)      # a watcher decides from this whether to retry
        # A rollout that stops answering is as transient as one answering 503,
        # and carries neither a status nor a close code to say so.
        err.transient = unanswered(exc)
        raise err from exc

    room_collab = RoomDoc(ws, doc, awareness, text_name, kind=kind)
    try:
        await room_collab._start()
    except BaseException:
        await connection.__aexit__(*sys.exc_info())
        raise
    try:
        yield room_collab
    finally:
        await room_collab._stop()
        await connection.__aexit__(None, None, None)
