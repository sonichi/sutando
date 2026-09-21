#!/usr/bin/env python3
"""A held document connection turns remote edits into "a new line names me".

Without this an agent connected to a document sees characters change and
nothing else; a person has to ping it in the room. Two halves: the pure rules
that find new lines and the ones addressed to a handle, and the client's
`changes()` iterator that feeds them from a live document.

Run: python3 tests/room-collab-watch.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Map, Text
except ImportError as exc:  # pragma: no cover
    print(f"room-collab watch: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_collab_client import RoomDoc  # noqa: E402
from room_collab_board import BOARD_KIND, ELEMENTS_KEY  # noqa: E402

from room_collab_protocol import (DEFAULT_KIND, DEFAULT_TEXT_NAME, RECONNECT_CODES,  # noqa: E402
                               RoomDocError)
from room_kanban import CARDS_KEY, KANBAN_KIND  # noqa: E402

from room_collab_watch import (addressed_to, board_mentions, kanban_changes,  # noqa: E402
                            new_lines, peer_changes)

FAILS = []


def check(name, fn):
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


# --- the pure rules

def test_new_lines_are_the_ones_that_appeared():
    assert new_lines("a\nb", "a\nb\nc") == ["c"]
    assert new_lines("a\nb", "z\na\nb") == ["z"], "position does not matter, presence does"
    assert new_lines("a", "a") == []


def test_a_line_written_a_second_time_is_new_again():
    assert new_lines("todo: x", "todo: x\ntodo: x") == ["todo: x"]


def test_blank_lines_are_never_new():
    assert new_lines("a", "a\n\n\n") == []


def test_addressed_to_matches_the_at_mention_as_a_whole_token():
    lines = ["@mars please look", "@marshall must not match", "for mars, not a mention",
             "cc @Mars in caps", "@sutando-qingyun-001:ag2.space by mxid", "@mars."]
    got = addressed_to(lines, ["mars", "@sutando-qingyun-001:ag2.space"])
    assert got == ["@mars please look", "cc @Mars in caps",
                   "@sutando-qingyun-001:ag2.space by mxid", "@mars."], got


def test_cjk_and_spaces_in_a_handle_match_literally():
    assert addressed_to(["@小火星 看一下", "@Mars the product dev: hi"],
                        ["小火星", "Mars the product dev"]) == \
        ["@小火星 看一下", "@Mars the product dev: hi"]


def test_no_handles_means_no_lines_not_all_lines():
    assert addressed_to(["@a", "@b"], []) == []


# --- the other documents and presence, pure

def test_a_board_label_that_newly_names_me_is_a_mention_and_a_moved_one_is_not():
    before = [{"id": "t1", "type": "text", "text": "@mars here", "x": 0},
              {"id": "r1", "type": "rectangle"}]
    after = [{"id": "t1", "type": "text", "text": "@mars here", "x": 50},
             {"id": "t2-new", "type": "text", "text": "ask @mars about this"},
             {"id": "t3-not-me", "type": "text", "text": "@marshall"},
             {"id": "t4-gone", "type": "text", "text": "@mars", "isDeleted": True}]
    got = board_mentions(before, after, ["mars"])
    assert [e["element"] for e in got] == ["t2-new"], got
    assert got[0]["kind"] == "mention" and got[0]["where"] == "board"


def test_a_card_assigned_to_me_moved_or_taken_away():
    me = "@mars:x"
    before = {"c1": {"id": "c1", "column": "todo", "assignee": me},
              "c2": {"id": "c2", "column": "todo", "assignee": "@other:x"},
              "c3": {"id": "c3", "column": "doing", "assignee": me}}
    after = {"c1": {"id": "c1", "column": "doing", "assignee": me},
             "c2": {"id": "c2", "column": "todo", "assignee": me},
             "c3": {"id": "c3", "column": "doing", "assignee": me, "deleted": True},
             "c4-not-mine": {"id": "c4", "column": "todo", "assignee": "@other:x"}}
    kinds = {e["card"]: e["kind"] for e in kanban_changes(before, after, [me])}
    assert kinds == {"c1": "moved", "c2": "assigned", "c3": "unassigned"}, kinds
    moved = [e for e in kanban_changes(before, after, [me]) if e["kind"] == "moved"][0]
    assert (moved["from"], moved["to"]) == ("todo", "doing")


def test_assignee_matches_with_or_without_the_at_and_case():
    after = {"c": {"id": "c", "column": "x", "assignee": "Mars"}}
    assert kanban_changes({}, after, ["@mars"])[0]["kind"] == "assigned"
    assert kanban_changes({}, after, ["marshall"]) == []
    junk = {"c": "not a card", "d": {"id": "d", "column": "x", "assignee": 7}}
    assert kanban_changes({}, junk, ["mars"]) == [], "junk in the map is skipped, not raised on"


def test_peers_arriving_and_leaving_by_identity_not_device():
    before = [{"id": "@q:x", "name": "qingyun"}]
    after = [{"id": "@q:x", "name": "qingyun"}, {"id": "@q:x", "name": "qingyun (phone)"},
             {"id": "@e:x", "name": "echo"}]
    got = peer_changes(before, after)
    assert [(e["kind"], e["who"]) for e in got] == [("peer_joined", "@e:x")], got
    assert [(e["kind"], e["who"]) for e in peer_changes(after, [])] == \
        [("peer_left", "@q:x"), ("peer_left", "@e:x")]


# --- the live half: changes() on a document edited by a remote peer

class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make():
    doc = Doc()
    doc.get(DEFAULT_TEXT_NAME, type=Text)
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=DEFAULT_KIND)


def remote_append(local: Doc, text: str) -> None:
    """What a peer's edit looks like when it arrives: an update applied to
    our doc with no local origin."""
    peer = Doc()
    peer.apply_update(local.get_update())
    peer.get(DEFAULT_TEXT_NAME, type=Text).__iadd__(text)
    local.apply_update(peer.get_update(local.get_state()))


async def test_a_remote_edit_is_yielded_as_the_new_text():
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    remote_append(doc, "hello @mars")
    got = await asyncio.wait_for(nxt, 2)
    assert got == "hello @mars", got
    await agen.aclose()


async def test_the_agents_own_write_is_not_reported():
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    await room.append("mine")            # local origin
    remote_append(doc, " theirs")       # then a real remote edit
    got = await asyncio.wait_for(nxt, 2)
    assert got == "mine theirs", "only the remote edit should wake the watcher"
    assert nxt.done()
    await agen.aclose()


async def test_the_session_ending_raises_instead_of_stopping_quietly():
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    room._ended.set_result(None)
    try:
        await asyncio.wait_for(nxt, 2)
    except RoomDocError as e:
        assert "ended" in str(e)
    else:
        raise AssertionError("a closed session must raise, not look like silence")


async def test_closing_the_iterator_leaves_the_session_alive():
    """The first version waited on the session future directly, so closing
    the iterator cancelled the session and the next write raised."""
    doc, room = make()
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    nxt.cancel()
    try:
        await nxt
    except asyncio.CancelledError:
        pass
    await agen.aclose()
    assert not room._ended.done(), "closing the watcher must not end the session"
    await room.append("still writable")
    assert room.text == "still writable"


async def test_settle_turns_a_burst_of_keystrokes_into_one_text():
    """Measured on production: one push per keystroke, no batching. Without
    coalescing a watcher prints the mention line once per character typed."""
    doc, room = make()
    agen = room.changes(settle=0.15)
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    for ch in "@mars hi":                       # eight pushes, 20 ms apart
        remote_append(doc, ch)
        await asyncio.sleep(0.02)
    assert not nxt.done(), "nothing should be yielded while pushes keep coming"
    got = await asyncio.wait_for(nxt, 2)
    assert got == "@mars hi", got
    # and nothing else is queued behind it
    nxt2 = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.3)
    assert not nxt2.done(), "the burst must yield exactly once"
    nxt2.cancel()
    try:
        await nxt2
    except asyncio.CancelledError:
        pass
    await agen.aclose()


async def test_without_settle_every_push_is_yielded():
    doc, room = make()
    agen = room.changes()
    got = []
    async def take(n):
        async for text in agen:
            got.append(text)
            if len(got) == n:
                break
    task = asyncio.ensure_future(take(3))
    await asyncio.sleep(0)
    for ch in "abc":
        remote_append(doc, ch)
        await asyncio.sleep(0.01)
    await asyncio.wait_for(task, 2)
    assert got == ["a", "ab", "abc"], got
    await agen.aclose()


async def test_new_lines_through_changes_find_the_mention():
    """The whole path a watcher runs: snapshot, remote edit, diff, address."""
    doc, room = make()
    seen = room.text
    agen = room.changes()
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    remote_append(doc, "line one\n@mars line two\n@other line three")
    text = await asyncio.wait_for(nxt, 2)
    assert addressed_to(new_lines(seen, text), ["mars"]) == ["@mars line two"]
    await agen.aclose()


# --- events(): one stream, every kind

def make_kind(kind):
    doc = Doc()
    if kind == DEFAULT_KIND:
        doc.get(DEFAULT_TEXT_NAME, type=Text)
    elif kind == BOARD_KIND:
        doc.get(ELEMENTS_KEY, type=Map)
    else:
        doc.get(CARDS_KEY, type=Map)
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=kind)


def remote_set(local: Doc, key: str, ident: str, value: dict) -> None:
    peer = Doc()
    peer.apply_update(local.get_update())
    peer.get(key, type=Map)[ident] = value
    local.apply_update(peer.get_update(local.get_state()))


async def first_event(room, handles, act):
    agen = room.events(handles, settle=0.05)
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    act()
    ev = await asyncio.wait_for(nxt, 2)
    await agen.aclose()
    return ev


async def test_events_on_text_yields_a_mention():
    doc, room = make_kind(DEFAULT_KIND)
    ev = await first_event(room, ["mars"], lambda: remote_append(doc, "hi\n@mars look"))
    assert ev == {"kind": "mention", "where": "text", "text": "@mars look"}, ev


async def test_events_on_the_board_yields_a_label_mention():
    doc, room = make_kind(BOARD_KIND)
    el = {"id": "t1", "type": "text", "x": 0, "y": 0, "width": 10, "height": 10,
          "version": 1, "text": "cc @mars"}
    ev = await first_event(room, ["mars"], lambda: remote_set(doc, ELEMENTS_KEY, "t1", el))
    assert ev["kind"] == "mention" and ev["where"] == "board" and ev["element"] == "t1", ev


async def test_events_on_the_kanban_yields_an_assignment():
    doc, room = make_kind(KANBAN_KIND)
    card = {"id": "c1", "column": "todo", "assignee": "@mars:x", "updated": 1, "text": "do it"}
    ev = await first_event(room, ["@mars:x"], lambda: remote_set(doc, CARDS_KEY, "c1", card))
    assert ev["kind"] == "assigned" and ev["card"] == "c1" and ev["text"] == "do it", ev


async def test_events_yields_nothing_for_edits_that_do_not_concern_me():
    doc, room = make_kind(DEFAULT_KIND)
    agen = room.events(["mars"], settle=0.05)
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    remote_append(doc, "@other please look")
    await asyncio.sleep(0.3)
    assert not nxt.done(), "an edit for someone else must not wake the watcher"
    nxt.cancel()
    try:
        await nxt
    except asyncio.CancelledError:
        pass
    await agen.aclose()


# --- coming back from a restart: a deploy closed a held connection at t+27m (1012)

class _Closed(Exception):
    def __init__(self, code):
        self.code = code


async def test_the_close_code_reaches_the_caller_with_the_last_snapshot():
    doc, room = make_kind(DEFAULT_KIND)
    agen = room.events(["mars"], settle=0.05)
    nxt = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    room._ended.set_result(_Closed(1012))
    try:
        await asyncio.wait_for(nxt, 2)
    except RoomDocError as e:
        assert e.code == 1012 and e.code in RECONNECT_CODES
        assert e.snapshot == {"peers": [], "text": ""}, "the snapshot travels with the error"
    else:
        raise AssertionError("must raise")


async def test_an_anchor_is_a_relative_position_pair_in_yjs_ids():
    import base64
    from pycrdt import StickyIndex
    doc, rd = make()
    text = doc.get(DEFAULT_TEXT_NAME, type=Text)
    # Built in four transactions so the items are out of clock order; the
    # expected IDs are the ones a Y.Doc resolves back to the same indexes.
    text += "héllo"
    text.insert(0, "¡")
    text += "日本"
    text.insert(3, "😀")
    s = rd.text
    assert s == "¡h😀éllo日本", s
    me = doc.client_id

    def decoded(b64):
        return StickyIndex.decode(base64.b64decode(b64), sequence=text).to_json()

    a = rd.anchor(s.index("日本"), s.index("日本") + 2)   # chars 7..9 = units 8..10
    assert decoded(a["start"]) == {"item": {"client": me, "clock": 6}, "assoc": 0}, a
    assert decoded(a["end"]) == {"tname": DEFAULT_TEXT_NAME, "assoc": 0}, a   # the end of the text
    b = rd.anchor(s.index("éllo"), s.index("éllo") + 4)   # chars 3..7 = units 4..8, after the emoji
    assert decoded(b["start"]) == {"item": {"client": me, "clock": 1}, "assoc": 0}, b
    assert decoded(b["end"]) == {"item": {"client": me, "clock": 6}, "assoc": 0}, b

    for bad in ((-1, 2), (3, 2), (0, len(s) + 1)):
        try:
            rd.anchor(*bad)
            raise AssertionError(f"accepted {bad}")
        except RoomDocError as e:
            assert "outside the text" in str(e)


def test_a_refusal_is_never_a_reconnect():
    from room_collab_protocol import close_code
    for refusal in (4400, 4403, 4404):
        assert refusal not in RECONNECT_CODES
    for restart in (1001, 1006, 1012):
        assert restart in RECONNECT_CODES
    assert close_code(None) is None and close_code(_Closed(1012)) == 1012


async def test_a_carried_snapshot_reports_what_landed_in_the_gap():
    """The watcher's document AFTER the restart already holds the line that
    arrived while it was down. Diffing against the carried snapshot, not the
    fresh one, is what reports it instead of skipping it."""
    doc, room = make_kind(DEFAULT_KIND)
    remote_append(doc, "@mars this landed during the deploy")   # present at reconnect
    before = {"peers": [], "text": ""}                             # what the old session had
    agen = room.events(["mars"], settle=0.05, since=before)
    ev = await asyncio.wait_for(agen.__anext__(), 2)
    assert ev["text"] == "@mars this landed during the deploy", ev
    await agen.aclose()
    fresh = room.events(["mars"], settle=0.05)                     # control: no carry, no event
    nxt = asyncio.ensure_future(fresh.__anext__())
    await asyncio.sleep(0.3)
    assert not nxt.done(), "without a carried snapshot the same line is old news"
    nxt.cancel()
    try:
        await nxt
    except asyncio.CancelledError:
        pass
    await fresh.aclose()


async def test_presence_stops_renewing_once_the_socket_is_dead():
    """After a close, the renewal loop sent on the dead socket every tick and
    logged a traceback each time — ten of them in the two minutes measured."""
    doc, room = make_kind(DEFAULT_KIND)
    class DeadWS:
        def __init__(self): self.calls = 0
        async def send(self, payload):
            self.calls += 1
            raise ConnectionError("closed")
    room._ws = DeadWS()
    room._ended.set_result(_Closed(1012))
    await room._send_quietly(b"x")
    assert room._ws.calls == 0, "nothing is sent once the session has ended"
    live_doc, live = make_kind(DEFAULT_KIND)
    live._ws = DeadWS()
    await live._send_quietly(b"x")                                # not ended: tried, and quiet
    assert live._ws.calls == 1


# --- the CLI loop: prints events, comes back from a restart, stops on a refusal

import contextlib  # noqa: E402
import io  # noqa: E402

import types  # noqa: E402

import room_collab as cli  # noqa: E402


class _Session:
    """A stand-in for RoomDoc: yields scripted events, then ends the way the
    script says — a restart code, a refusal, or a clean stop."""

    def __init__(self, script, end_code):
        self.script, self.end_code = script, end_code
        self.presence = None

    async def set_presence(self, name, user_id=None):
        self.presence = name

    async def events(self, handles, settle=1.0, since=None):
        for ev in self.script:
            yield dict(ev)
        if self.end_code is None:
            return
        err = RoomDocError(f"ended {self.end_code}")
        err.code = self.end_code
        err.snapshot = {"peers": [], "text": "carried"}
        raise err


def _fake_opener(sessions):
    """Each call opens the next scripted session, or raises it when the script
    queued an exception — a handshake the edge refused."""
    calls = []

    @contextlib.asynccontextmanager
    async def open_room_collab(url, room, token, kind=None, insecure=False):
        calls.append((url, room, kind))
        nxt = sessions.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        yield nxt

    return open_room_collab, calls


def _refused_open(status):
    err = RoomDocError(f"handshake refused {status}")
    err.status = status
    return err


def _args(**over):
    base = dict(room="!r:x", kind="markdown", handles=["mars"], settle=0.01, name="mars",
                user_id=None, insecure=False, max_reconnects=3)
    base.update(over)
    return types.SimpleNamespace(**base)


async def _run_watch(sessions, **over):
    opener, calls = _fake_opener(sessions)
    out = io.StringIO()
    import room_collab_client
    real = room_collab_client.open_room_collab
    room_collab_client.open_room_collab = opener
    try:
        with contextlib.redirect_stdout(out):
            rc = await cli.watch(_args(**over), "tok", "https://h")
    except RoomDocError as e:
        rc = e
    finally:
        room_collab_client.open_room_collab = real
    return rc, out.getvalue(), calls


async def test_watch_prints_one_line_per_event_and_stops_cleanly():
    rc, out, calls = await _run_watch([_Session(
        [{"kind": "mention", "where": "text", "text": "@mars hi"},
         {"kind": "assigned", "where": "kanban", "card": "c1", "column": "todo", "text": "do"}],
        None)])
    assert rc == 0 and len(calls) == 1
    assert "EVENT\tmention\twhere=text\t@mars hi" in out, out
    assert "EVENT\tassigned\twhere=kanban card=c1 column=todo\tdo" in out, out


async def test_watch_comes_back_from_a_restart_and_carries_the_snapshot():
    first = _Session([{"kind": "mention", "where": "text", "text": "@mars before"}], 1012)
    second = _Session([{"kind": "mention", "where": "text", "text": "@mars after"}], None)
    rc, out, calls = await _run_watch([first, second])
    assert rc == 0 and len(calls) == 2, "one reconnect, then a clean end"
    assert "RECONNECTING\tcode=1012 attempt=1" in out and "RECONNECTED" in out, out
    assert out.index("@mars before") < out.index("RECONNECTING") < out.index("@mars after")
    assert second.presence == "mars", "presence is re-published on the new socket"


async def test_watch_does_not_retry_a_refusal():
    rc, out, calls = await _run_watch([_Session([], 4403)])
    assert isinstance(rc, RoomDocError) and rc.code == 4403 and len(calls) == 1
    assert "RECONNECTING" not in out


async def test_watch_gives_up_after_max_reconnects():
    sessions = [_Session([], 1012) for _ in range(5)]
    rc, out, calls = await _run_watch(sessions, max_reconnects=2)
    assert isinstance(rc, RoomDocError) and rc.code == 1012
    assert len(calls) == 3, "the first open plus two reconnects, then stop"


async def test_watch_rides_out_a_rejected_handshake_during_a_rollout():
    # The real sequence from a deploy: the socket closes 1012, the reopen is
    # refused 502 while the new pod comes up, then the service is back.
    first = _Session([{"kind": "mention", "where": "text", "text": "@mars before"}], 1012)
    back = _Session([{"kind": "mention", "where": "text", "text": "@mars after"}], None)
    rc, out, calls = await _run_watch([first, _refused_open(502), _refused_open(503), back])
    assert rc == 0 and len(calls) == 4, (rc, calls)
    assert "RECONNECTING\tcode=1012 attempt=1" in out, out
    assert "RECONNECTING\tstatus=502 attempt=2" in out, out
    assert "RECONNECTING\tstatus=503 attempt=3" in out, out
    assert out.index("@mars before") < out.index("status=502") < out.index("@mars after"), out


async def test_watch_does_not_retry_a_refused_handshake_that_is_not_a_rollout():
    rc, out, calls = await _run_watch([_refused_open(404)])
    assert isinstance(rc, RoomDocError) and rc.status == 404 and len(calls) == 1
    assert "RECONNECTING" not in out


async def test_a_refused_handshake_carries_its_status_out_of_the_client():
    # The real opener: websockets rejects the upgrade with the response attached.
    import room_collab_client
    from room_collab_client import open_room_collab

    class _Resp:
        status_code = 503
        body = b"upstream connect error"

    class _Rejected(Exception):
        response = _Resp()

    class _Connect:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise _Rejected("server rejected WebSocket connection: HTTP 503")

    real = room_collab_client.websockets.connect
    room_collab_client.websockets.connect = _Connect
    try:
        async with open_room_collab("https://h", "!r:x", "tok"):
            raise AssertionError("opened")
    except RoomDocError as e:
        assert e.status == 503 and "not being served right now (503)" in str(e), (e.status, str(e))
    finally:
        room_collab_client.websockets.connect = real


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab watch: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab watch: ok")
