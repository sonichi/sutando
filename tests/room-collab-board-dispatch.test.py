#!/usr/bin/env python3
"""Which client call each board subcommand makes — the CLI's dispatch.

`render()` and `parse_elements()` are pure and tested elsewhere; this covers the
part between them, where a subcommand is turned into a call on an open document.
Getting it wrong is how `peers` ended up refused on a board: the dispatch, not
the rules, decided it.

A stand-in document stands in for the socket, so no server is needed.
Run: python3 tests/room-collab-board-dispatch.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import contextlib
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

import room_collab  # noqa: E402
import room_collab_client  # noqa: E402

from room_collab_protocol import RoomDocError  # noqa: E402

FAILS = []


class FakeDoc:
    def __init__(self):
        self.calls = []
        self.elements = [{"id": "a", "type": "rectangle", "x": 0, "y": 0,
                          "width": 1, "height": 1, "version": 1}]
        self.peers = [{"name": "someone"}]
        self.text = "hello"

    async def set_presence(self, name, user_id=None):
        self.calls.append(("presence", name))

    async def put_elements(self, elements):
        self.calls.append(("put", [e.get("id") for e in elements]))
        self.written = elements
        return len(elements)

    async def delete_element(self, element_id):
        self.calls.append(("delete", element_id))

    async def settle(self, seconds):
        self.calls.append(("settle", seconds))

    async def append(self, text):
        self.calls.append(("append", text))

    async def replace(self, old, new):
        self.calls.append(("replace", old, new))


def run_cli(argv, doc):
    """Drive the real CLI against a stand-in document."""
    @contextlib.asynccontextmanager
    async def fake_open(url, room, token, *, kind="markdown", insecure=False):
        doc.opened_kind = kind
        yield doc

    real = room_collab_client.open_room_collab
    room_collab_client.open_room_collab = fake_open
    out, err = io.StringIO(), io.StringIO()
    try:
        # main(), not run(): a refusal is turned into an exit code there, and
        # the exit code is what a caller actually sees.
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = room_collab.main(argv)
        return rc, out.getvalue()
    finally:
        room_collab_client.open_room_collab = real


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except SystemExit as e:
        # argparse rejects an unknown flag by exiting; that is this case's
        # failure, not a reason for the whole suite to stop unreported.
        FAILS.append(f"{name}: the CLI exited {e.code} instead of running")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


BASE = ["--url", "https://h", "--token", "t"]


def test_peers_on_a_board_is_answered_not_refused():
    doc = FakeDoc()
    rc, out = run_cli(BASE + ["--kind", "board", "peers", "!r:s"], doc)
    assert rc == 0, rc
    assert json.loads(out) == [{"name": "someone"}], out
    assert doc.opened_kind == "board"


def test_draw_parses_then_writes_and_settles():
    doc = FakeDoc()
    rc, out = run_cli(
        BASE + ["--kind", "board", "draw", "!r:s", '[{"id":"z"}]'], doc)
    assert rc == 0
    assert ("put", ["z"]) in doc.calls, doc.calls
    assert any(c[0] == "settle" for c in doc.calls), "a write must settle"
    assert json.loads(out)["ok"] is True


ON_TOP = ('[{"id":"z","type":"rectangle","x":0,"y":0,'
          '"width":10,"height":10,"version":1}]')


def test_draw_moves_a_drawing_off_what_is_already_there():
    """The stand-in board holds one element at (0,0); a draw at (0,0) without
    looking is exactly the owner's overlaid-diagrams screenshot."""
    doc = FakeDoc()
    rc, _ = run_cli(BASE + ["--kind", "board", "draw", "!r:s", ON_TOP], doc)
    assert rc == 0
    assert doc.written[0]["y"] > 1, f"still on top: {doc.written[0]}"
    assert doc.written[0]["x"] == 0, "only y moves"
    assert isinstance(doc.written[0]["y"], int)


def test_absolute_writes_the_coordinates_as_given():
    """The escape for a caller that looked: what it asked for is what lands."""
    doc = FakeDoc()
    rc, _ = run_cli(
        BASE + ["--kind", "board", "draw", "--absolute", "!r:s", ON_TOP], doc)
    assert rc == 0
    assert doc.written[0]["y"] == 0, doc.written[0]


def test_draw_reads_the_board_before_deciding():
    """Control for the above: with nothing on the board, nothing moves — so
    the shift in the first case came from what was read, not from a constant."""
    doc = FakeDoc()
    doc.elements = []
    rc, _ = run_cli(BASE + ["--kind", "board", "draw", "!r:s", ON_TOP], doc)
    assert rc == 0
    assert doc.written[0]["y"] == 0


def test_erase_deletes_by_id():
    doc = FakeDoc()
    rc, _ = run_cli(BASE + ["--kind", "board", "erase", "!r:s", "a"], doc)
    assert rc == 0
    assert ("delete", "a") in doc.calls, doc.calls


def test_a_text_command_on_a_board_is_refused_by_the_dispatch():
    doc = FakeDoc()
    rc, _ = run_cli(BASE + ["--kind", "board", "append", "!r:s", "x"], doc)
    assert rc == 2, "a refusal exits non-zero"
    assert not any(c[0] == "append" for c in doc.calls), "nothing may be written"


def test_a_board_command_without_the_board_is_refused():
    doc = FakeDoc()
    rc, _ = run_cli(BASE + ["draw", "!r:s", "[]"], doc)
    assert rc == 2
    assert not any(c[0] == "put" for c in doc.calls)


def test_the_text_document_still_dispatches_normally():
    """Control: the board branch must not have swallowed the text path."""
    doc = FakeDoc()
    rc, out = run_cli(BASE + ["append", "!r:s", "more"], doc)
    assert rc == 0
    assert ("append", "more") in doc.calls, doc.calls
    doc2 = FakeDoc()
    rc2, out2 = run_cli(BASE + ["read", "!r:s"], doc2)
    assert rc2 == 0 and out2.strip() == "hello", out2


def test_presence_is_published_when_a_name_is_given():
    doc = FakeDoc()
    run_cli(BASE + ["--name", "mars", "--kind", "board", "read", "!r:s"], doc)
    assert ("presence", "mars") in doc.calls, doc.calls


def _snapshot_file(body):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(body, f); f.close()
    return f.name


def test_snapshot_writes_the_board_to_a_file():
    import tempfile
    doc = FakeDoc()
    out = tempfile.mktemp(suffix=".json")
    rc, _ = run_cli(BASE + ["--kind", "board", "snapshot", "!r:s", "--out", out], doc)
    body = json.loads(open(out).read())
    assert rc == 0 and body["room"] == "!r:s" and body["surface"] == "board", body
    assert [e["id"] for e in body["elements"]] == ["a"], body


def test_restore_is_a_dry_run_unless_applied():
    snap = _snapshot_file({"room": "!r:s", "surface": "board",
                           "elements": [{"id": "lost", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1, "version": 3}]})
    doc = FakeDoc()
    rc, out = run_cli(BASE + ["--kind", "board", "restore", "!r:s", snap], doc)
    assert rc == 0 and not any(c[0] == "put" for c in doc.calls), doc.calls
    assert json.loads(out)["would_write"] == 1, out
    doc = FakeDoc()
    rc, out = run_cli(BASE + ["--kind", "board", "restore", "!r:s", snap, "--apply"], doc)
    assert rc == 0 and ("put", ["lost"]) in doc.calls, doc.calls


def test_restore_refuses_another_rooms_snapshot():
    snap = _snapshot_file({"room": "!other:s", "surface": "board", "elements": []})
    doc = FakeDoc()
    rc, _ = run_cli(BASE + ["--kind", "board", "restore", "!r:s", snap, "--apply"], doc)
    assert rc != 0 and not any(c[0] == "put" for c in doc.calls), doc.calls


def test_an_edit_of_an_element_changed_since_reading_is_refused():
    """The board holds "a" at v1. Sending v1 again (a tie) or v0 means the writer
    read an older state: refuse, write nothing, name the element."""
    for sent in (1, 0):
        doc = FakeDoc()
        el = json.dumps([{"id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1, "version": sent}])
        rc, out = run_cli(BASE + ["--kind", "board", "draw", "!r:s", el], doc)
        assert rc != 0, f"v{sent} over a stored v1 must be refused"
        assert not any(c[0] == "put" for c in doc.calls), doc.calls


def test_the_next_version_is_an_ordinary_edit():
    doc = FakeDoc()
    el = json.dumps([{"id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1, "version": 2}])
    rc, _ = run_cli(BASE + ["--kind", "board", "draw", "!r:s", el], doc)
    assert rc == 0 and ("put", ["a"]) in doc.calls, doc.calls


def test_force_overwrites_a_stale_edit():
    doc = FakeDoc()
    el = json.dumps([{"id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1, "version": 1}])
    rc, _ = run_cli(BASE + ["--kind", "board", "draw", "!r:s", el, "--force"], doc)
    assert rc == 0 and ("put", ["a"]) in doc.calls, doc.calls


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab board dispatch: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab board dispatch: ok")
