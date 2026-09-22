#!/usr/bin/env python3
"""The kanban commands, driven through the real CLI against a stand-in board.

Which command writes what, which refusals fire, and whose mxid a write is
signed with — each a way an agent's card silently never appears.

Run: python3 tests/room-collab-kanban-dispatch.test.py  (exit 0 pass / 1 fail)
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

try:
    import room_collab  # noqa: E402
    import room_collab_client  # noqa: E402
except ImportError as exc:
    print(f"room-collab client needs its dependencies: {exc}")
    print("install them with: pip install -r skills/room-collab/requirements.txt")
    sys.exit(0)

from room_collab import resolve_identity  # noqa: E402
from room_collab_protocol import RoomDocError  # noqa: E402

FAILS = []
BY = "@sutando-x:ag2.space"


def card(ident="c1", column="todo", text="ship it", **over):
    base = {"id": ident, "column": column, "order": 1024, "text": text,
            "assignee": "", "updated": 1000, "by": "@a:x"}
    base.update(over)
    return base


class FakeDoc:
    """What kanban() reads and writes: columns and cards as (id, record) pairs."""

    def __init__(self, columns=None, cards=None):
        self.calls = []
        self.columns = [] if columns is None else columns
        self._cards = {} if cards is None else {c["id"]: c for c in cards}
        self.peers = [{"name": "someone"}]
        self.written = []

    @property
    def cards(self):
        return list(self._cards.items())

    async def set_presence(self, name, user_id=None):
        self.calls.append(("presence", name))

    async def put_columns(self, columns):
        self.calls.append(("put_columns", [c["id"] for c in columns]))
        self.columns = list(columns)
        return len(columns)

    async def put_cards(self, cards):
        self.calls.append(("put_cards", [c["id"] for c in cards]))
        self.written = list(cards)
        for c in cards:
            self._cards[c["id"]] = c
        return len(cards)

    async def settle(self, seconds):
        self.calls.append(("settle", seconds))


def run_cli(argv, doc):
    @contextlib.asynccontextmanager
    async def fake_open(url, room, token, *, kind="markdown", insecure=False):
        doc.opened_kind = kind
        yield doc

    real = room_collab_client.open_room_collab
    room_collab_client.open_room_collab = fake_open
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = room_collab.main(argv)
        return rc, out.getvalue(), err.getvalue()
    finally:
        room_collab_client.open_room_collab = real


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except SystemExit as e:
        FAILS.append(f"{name}: the CLI exited {e.code} instead of running")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


BASE = ["--url", "https://h", "--token", "t", "--kind", "kanban", "--user-id", BY]
COLS = [{"id": "todo", "title": "To do", "order": 0, "updated": 1, "by": "@a:x"},
        {"id": "doing", "title": "Doing", "order": 1024, "updated": 1, "by": "@a:x"}]


# --- who a write is signed with: explicit, else the environment, else a refusal

def test_identity_is_explicit_then_env_then_refused():
    assert resolve_identity("@me:x") == "@me:x"
    saved = {v: os.environ.pop(v, None) for v in room_collab.IDENTITY_VARS}
    try:
        try:
            resolve_identity(None)
        except RoomDocError as exc:
            assert "--user-id" in str(exc), exc
        else:
            raise AssertionError("no identity anywhere must be a refusal, not an empty `by`")
        os.environ[room_collab.IDENTITY_VARS[0]] = "@env:x"
        assert resolve_identity(None) == "@env:x", "the environment supplies it"
    finally:
        for v, old in saved.items():
            if old is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = old


# --- read: the board as text and as JSON, orphans named rather than dropped

def test_read_prints_columns_cards_and_orphans():
    doc = FakeDoc(COLS, [card("c1"), card("c2", "doing", "review"), card("lost", "nowhere", "?")])
    rc, out, _ = run_cli(BASE + ["read", "!r:s"], doc)
    assert rc == 0, rc
    assert "c1" in out and "review" in out
    assert "(no column)" in out and "nowhere" in out, "a card whose column is gone is shown, not lost"
    rc, out, _ = run_cli(BASE + ["--json", "read", "!r:s"], doc)
    assert rc == 0
    payload = json.loads(out)
    assert {c["id"] for c in payload["cards"]} >= {"c1", "c2"}
    assert doc.opened_kind == "kanban"


def test_peers_on_a_kanban_is_answered():
    rc, out, _ = run_cli(BASE + ["peers", "!r:s"], FakeDoc(COLS))
    assert rc == 0 and json.loads(out) == [{"name": "someone"}]


# --- add: seeds the panel's columns on an empty board, refuses a column it has not got

def test_add_seeds_default_columns_then_writes_a_signed_card():
    doc = FakeDoc()
    rc, out, _ = run_cli(BASE + ["add", "!r:s", "write the notes", "--id", "n1"], doc)
    assert rc == 0, rc
    assert doc.calls[0][0] == "put_columns", "an empty board is seeded first"
    assert {c["id"] for c in doc.columns} >= {"todo", "doing", "done"}
    written = doc.written[0]
    assert written["id"] == "n1" and written["column"] == "todo" and written["by"] == BY
    assert json.loads(out)["ok"] is True
    assert any(c[0] == "settle" for c in doc.calls), "a write must settle"


def test_add_into_a_named_column_with_an_assignee():
    doc = FakeDoc(COLS)
    rc, _, _ = run_cli(BASE + ["add", "!r:s", "x", "--column", "doing", "--assign", "@m:x"], doc)
    assert rc == 0
    assert doc.written[0]["column"] == "doing" and doc.written[0]["assignee"] == "@m:x"
    assert doc.calls[0][0] == "put_cards", "columns already exist: none seeded"


def test_add_refuses_a_column_the_board_has_not_got():
    doc = FakeDoc(COLS)
    rc, out, err = run_cli(BASE + ["add", "!r:s", "x", "--column", "later"], doc)
    assert rc != 0, "a card into no column must be a refusal"
    assert "later" in err and "todo" in err, "the refusal names the bad column and the real ones"
    assert doc.written == [], "nothing written"


# --- move / assign / erase act on a live card and write a newer, signed version

def test_move_writes_the_card_in_its_new_column():
    doc = FakeDoc(COLS, [card("c1")])
    rc, out, _ = run_cli(BASE + ["move", "!r:s", "c1", "doing"], doc)
    assert rc == 0, rc
    w = doc.written[0]
    assert w["column"] == "doing" and w["by"] == BY and w["updated"] > 1000
    assert json.loads(out)["card"]["id"] == "c1"


def test_move_refuses_an_unknown_column():
    doc = FakeDoc(COLS, [card("c1")])
    rc, _, err = run_cli(BASE + ["move", "!r:s", "c1", "later"], doc)
    assert rc != 0 and "later" in err and doc.written == []


def test_assign_and_erase_write_newer_versions():
    doc = FakeDoc(COLS, [card("c1")])
    rc, _, _ = run_cli(BASE + ["assign", "!r:s", "c1", "@m:x"], doc)
    assert rc == 0 and doc.written[0]["assignee"] == "@m:x"
    rc, _, _ = run_cli(BASE + ["erase", "!r:s", "c1"], doc)
    assert rc == 0 and doc.written[0].get("deleted") is True, "erase is a tombstone, not a removal"


def test_a_missing_or_deleted_card_is_refused_not_invented():
    doc = FakeDoc(COLS, [card("gone", deleted=True)])
    for argv in (["move", "!r:s", "nope", "doing"], ["assign", "!r:s", "gone", "@m:x"]):
        rc, _, err = run_cli(BASE + argv, doc)
        assert rc != 0, argv
        assert "no live card" in err, err
    assert doc.written == []


# --- a text command on a kanban is refused by name, so the wrong kind is loud

def test_a_text_command_is_refused_on_a_kanban():
    rc, _, err = run_cli(BASE + ["append", "!r:s", "hello"], FakeDoc(COLS))
    assert rc != 0
    assert "kanban" in err.lower() or "append" in err, err


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab kanban dispatch: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab kanban dispatch: ok")
