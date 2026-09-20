#!/usr/bin/env python3
"""An agent reads and writes the board of cards through the panel's own rules.

The panel already tells an assigned agent to run `room_doc.py --kind kanban
read`; until now that command did not exist. The rules that matter here are
the ones a viewer enforces silently: a card missing a required field is not
refused by the panel, it is filtered out — so the writer must refuse it first.

Run: python3 tests/room-doc-kanban-client.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Map
except ImportError as exc:  # pragma: no cover
    print(f"room-doc kanban client: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_doc_client import RoomDoc  # noqa: E402
from room_doc_protocol import DEFAULT_TEXT_NAME, RoomDocError  # noqa: E402

from room_kanban import (CARDS_KEY, KANBAN_KIND, assign_card, default_columns,  # noqa: E402
                         move_card, new_card, order_after_last, order_between)

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make():
    doc = Doc()
    doc.get(CARDS_KEY, type=Map)
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=KANBAN_KIND)


def check(name, fn):
    try:
        asyncio.run(fn())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


ME = "@mars:x"


async def test_a_card_the_panel_would_drop_is_refused_before_it_is_written():
    _, board = make()
    incomplete = {"id": "c1", "column": "todo", "order": 0, "text": "x", "updated": 1, "by": ME}
    try:
        await board.put_cards([incomplete])           # no assignee key
    except RoomDocError as e:
        assert "kanban card" in str(e) and "Nothing was written" in str(e)
    else:
        raise AssertionError("must refuse")
    assert board.cards == [] and board._ws.sent == []


async def test_seeded_columns_are_the_panels_own():
    _, board = make()
    assert board.columns == []
    assert await board.put_columns(default_columns(1, ME)) == 3
    assert [c["id"] for c in board.columns] == ["todo", "doing", "done"]
    assert [c["order"] for c in board.columns] == [0, 1024, 2048], "the panel's spacing"


async def test_add_move_assign_erase_are_each_a_newer_version():
    _, board = make()
    await board.put_columns(default_columns(1, ME))
    card = new_card("c1", "todo", "write tests", 10, ME, order_after_last(board.cards, "todo"))
    assert card["order"] == 0 and card["assignee"] == ""
    assert await board.put_cards([card]) == 1
    second = new_card("c2", "todo", "next", 11, ME, order_after_last(board.cards, "todo"))
    assert second["order"] == 1024, "appended after the last, with the panel's gap"
    await board.put_cards([second])
    moved = move_card(card, "doing", order_after_last(board.cards, "doing"), 20, ME)
    assert await board.put_cards([moved]) == 1
    assert dict(board.cards[0][1])["column"] == "doing"
    assigned = assign_card(moved, "@you:x", 30, ME)
    await board.put_cards([assigned])
    assert dict(board.cards[0][1])["assignee"] == "@you:x"
    stale = move_card(card, "done", 0, 5, ME)                 # older than what is stored
    assert await board.put_cards([stale]) == 0, "an older version writes nothing"


async def test_a_card_read_back_is_written_back_with_integers():
    _, board = make()
    await board.put_cards([new_card("c1", "todo", "x", 10, ME, 1024)])
    stored = dict(board.cards[0][1])
    assert isinstance(stored["order"], float), "premise: the CRDT hands back floats"
    await board.put_cards([assign_card(stored, "@you:x", 20, ME)])
    raw = board._doc.get(CARDS_KEY, type=Map)["c1"]
    assert isinstance(dict(raw)["order"], (int, float)) and dict(raw)["order"] == 1024


async def test_between_two_neighbours_is_the_panels_midpoint():
    assert order_between(None, None) == 0
    assert order_between(2048, None) == 3072
    assert order_between(None, 1024) == 0
    assert order_between(0, 1024) == 512
    assert order_between(0, 1) == 0, "an integer, even when squeezed"


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc kanban client: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc kanban client: ok")
