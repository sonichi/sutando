#!/usr/bin/env python3
"""Session lifecycle for skills/room-collab, against a real WebSocket server.

These cover what the codec tests cannot: the states where the client can be
WRONG WITHOUT ERRORING — a refusal read back as an empty document, a session
that ended still accepting writes, presence that stopped being renewed. The
service accepts the socket and only then closes with 4400/4403/4404, so every
one of those arrives as a mid-stream close rather than a handshake failure.

Skips when pycrdt/websockets are absent; nothing here reaches the network.
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    import websockets
    from pycrdt import Doc, Text, YMessageType, create_sync_message, handle_sync_message
except ImportError as exc:  # pragma: no cover
    print(f"room-collab lifecycle: SKIP (dependencies absent: {exc})")
    sys.exit(0)

from room_collab_client import RoomDoc, open_room_collab  # noqa: E402
from room_collab_protocol import RoomDocError, doc_socket_url  # noqa: E402

FAILS = []


def check(name, coro_fn):
    try:
        asyncio.run(asyncio.wait_for(coro_fn(), timeout=25))
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


class Server:
    """A room-collab stand-in: serves one document, or refuses like the real one."""

    def __init__(self, *, refuse_code=None, refuse_after_sync=False,
                 close_before_step2=False, seed=""):
        self.refuse_code = refuse_code
        self.refuse_after_sync = refuse_after_sync
        self.close_before_step2 = close_before_step2
        self.doc = Doc()
        self.text = self.doc.get("markdown", type=Text)
        if seed:
            self.text += seed
        self.paths = []
        self.awareness_frames = 0

    async def handler(self, ws):
        self.paths.append(ws.request.path)
        if self.close_before_step2:
            # Send succeeds, then the close lands: only the sync/ended split
            # tells this from a synced empty document.
            await ws.recv()
            await ws.close(self.refuse_code or 4403)
            return
        if self.refuse_code and not self.refuse_after_sync:
            await ws.close(self.refuse_code)
            return
        await ws.send(create_sync_message(self.doc))
        async for raw in ws:
            data = raw if isinstance(raw, bytes) else raw.encode()
            if data[0] == YMessageType.SYNC:
                reply = handle_sync_message(data[1:], self.doc)
                if reply is not None:
                    await ws.send(reply)
                if self.refuse_after_sync:
                    await ws.close(self.refuse_code)
                    return
            elif data[0] == YMessageType.AWARENESS:
                self.awareness_frames += 1

    async def __aenter__(self):
        self._srv = await websockets.serve(self.handler, "127.0.0.1", 0)
        self.port = self._srv.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self._srv.close()
        await self._srv.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/api/v1/room-doc"


async def test_a_refusal_after_accept_is_not_an_empty_document():
    """The bug this pins: 4400 once read back as 'a room with no content'."""
    async with Server(refuse_code=4400) as s:
        try:
            async with open_room_collab(s.url, "!bad:x", "tok"):
                raise AssertionError("opening must fail, not yield an empty document")
        except RoomDocError as e:
            assert "4400" in str(e), f"the reason must name the code, got: {e}"


async def test_a_close_before_step2_is_not_a_synced_empty_document():
    """Isolates the sync-vs-ended distinction: the send SUCCEEDS here, so the
    only thing standing between a refusal and a silent empty document is that
    a dead reader is not treated as a finished sync."""
    async with Server(close_before_step2=True, refuse_code=4403) as s:
        try:
            async with open_room_collab(s.url, "!r:x", "tok") as doc:
                raise AssertionError(
                    f"opening must fail; got a document of {len(doc.text)} chars")
        except RoomDocError as e:
            assert "4403" in str(e), e


async def test_a_bad_kind_refusal_is_also_surfaced():
    async with Server(refuse_code=4404) as s:
        try:
            async with open_room_collab(s.url, "!r:x", "tok", kind="nope"):
                raise AssertionError("opening must fail")
        except RoomDocError as e:
            assert "4404" in str(e), e


async def test_a_real_document_opens_and_reads():
    """Control: the same path succeeds when the server does not refuse."""
    async with Server(seed="hello world") as s:
        async with open_room_collab(s.url, "!r:x", "tok") as doc:
            assert doc.text == "hello world", repr(doc.text)


async def test_a_session_closed_after_sync_refuses_later_writes():
    async with Server(refuse_code=4403, refuse_after_sync=True) as s:
        async with open_room_collab(s.url, "!r:x", "tok") as doc:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if doc._ended.done():
                    break
            try:
                await doc.append("written after the server refused")
                raise AssertionError("a write after the session ended must fail")
            except RoomDocError as e:
                assert "4403" in str(e), e


async def test_presence_keeps_being_renewed_not_sent_once():
    """Holding the socket is not enough: the server expires a silent peer."""
    async with Server() as s:
        async with open_room_collab(s.url, "!r:x", "tok") as doc:
            await doc.set_presence("mars")
            await asyncio.sleep(0.2)
            first = s.awareness_frames
            assert first >= 1, "the initial presence must reach the server"
            doc._awareness.set_local_state_field("user", {"name": "mars", "moved": 1})
            await asyncio.sleep(0.4)
            assert s.awareness_frames > first, (
                "a local awareness change must be forwarded; without the observer "
                "presence goes stale while the socket stays open")


async def test_presence_carries_a_user_id_only_when_given_one():
    """The roster resolves an avatar by id, never by display name. Absent id
    means no face, which is better than someone else's."""
    async with Server() as s:
        async with open_room_collab(s.url, "!r:x", "tok") as doc:
            await doc.set_presence("mars")
            plain = doc._awareness.get_local_state()["user"]
            assert "userId" not in plain, plain
            await doc.set_presence("mars", user_id="@mars:ag2space.local")
            named = doc._awareness.get_local_state()["user"]
            assert named["userId"] == "@mars:ag2space.local", named
            assert named["name"] == "mars" and named["kind"] == "agent", named


async def test_the_cli_runs_every_subcommand_against_a_real_socket():
    """The CLI is the surface an agent actually calls; argument parsing alone
    does not prove a subcommand reaches the document."""
    import contextlib
    import io
    import json as _json

    import room_collab as cli

    async with Server(seed="alpha beta") as s:
        async def run(argv):
            args = cli.build_parser().parse_args(
                ["--url", s.url, "--token", "t", "--settle", "0.05"] + argv)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = await cli.run(args)
            return rc, out.getvalue()

        rc, out = await run(["read", "!r:x"])
        assert rc == 0 and "alpha beta" in out, (rc, out)

        rc, out = await run(["--json", "read", "!r:x"])
        assert rc == 0 and _json.loads(out)["chars"] == len("alpha beta"), out

        rc, out = await run(["peers", "!r:x"])
        assert rc == 0 and _json.loads(out) == [], out

        rc, out = await run(["append", "!r:x", " gamma"])
        assert rc == 0 and _json.loads(out)["ok"] is True, out

        rc, out = await run(["replace", "!r:x", "alpha", "ALPHA"])
        assert rc == 0 and _json.loads(out)["ok"] is True, out

        rc, out = await run(["--name", "mars", "--user-id", "@mars:x", "read", "!r:x"])
        assert rc == 0, out


async def test_the_cli_reports_a_refusal_as_a_nonzero_exit():
    """An agent scripting this must be able to tell failure from an empty read."""
    import contextlib
    import io

    import room_collab as cli

    async with Server(refuse_code=4400) as s:
        args = cli.build_parser().parse_args(
            ["--url", s.url, "--token", "t", "read", "!bad:x"])
        try:
            await cli.run(args)
            raise AssertionError("a refusal must not return normally")
        except RoomDocError as e:
            assert "4400" in str(e), e



async def test_the_kind_reaches_the_server_in_the_url():
    async with Server() as s:
        async with open_room_collab(s.url, "!r:x", "tok", kind="board"):
            pass
        assert any("kind=board" in p for p in s.paths), s.paths
    async with Server() as s2:
        async with open_room_collab(s2.url, "!r:x", "tok"):
            pass
        assert not any("kind=" in p for p in s2.paths), (
            f"the default must stay bare for compatibility: {s2.paths}")


def sync_test_main_maps_a_failure_to_a_nonzero_exit():
    """`main()` owns its own loop, so this cannot run inside the async harness.
    An agent scripting the CLI needs the exit code to mean something."""
    import contextlib
    import io

    import room_collab as cli
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli.main(["--url", "not-a-url", "--token", "t", "read", "!r:x"])
    assert rc != 0, "a failure must not exit 0"
    assert "room-collab:" in err.getvalue(), err.getvalue()


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

try:
    sync_test_main_maps_a_failure_to_a_nonzero_exit()
except AssertionError as e:
    FAILS.append(f"sync_test_main_maps_a_failure_to_a_nonzero_exit: {e}")

if FAILS:
    print("room-collab lifecycle: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab lifecycle: ok")
