#!/usr/bin/env python3
"""An agent can see who wrote what, and cannot claim it wrote something.

This is the half the UI cannot cover: a person reads colours, an agent reads
this. It decides whether to continue someone's paragraph or leave it alone —
"a human wrote this" and "another agent wrote this" call for different
behaviour, and the owner asked for exactly that distinction.

The map is the SERVER's. This client reads it; writing to it would be claiming
an identity rather than reporting one, so there is no writer here to test.
Run: python3 tests/room-doc-authors.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

try:
    from pycrdt import Awareness, Doc, Map
except ImportError as exc:  # pragma: no cover
    print(f"room-doc authors: FAIL — dependencies missing ({exc}).")
    sys.exit(1)

from room_doc import render  # noqa: E402

from room_doc_client import AUTHORS_KEY, RoomDoc  # noqa: E402
from room_doc_protocol import DEFAULT_KIND, DEFAULT_TEXT_NAME  # noqa: E402

FAILS = []

HUMAN = {"mxid": "@qingyun:ag2.space", "kind": "human"}
AGENT = {"mxid": "@mars:ag2space.local", "kind": "agent",
         "owner_mxid": "@qingyun:ag2.space"}


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make(rows=None):
    doc = Doc()
    if rows:
        authors = doc.get(AUTHORS_KEY, type=Map)
        for client, row in rows.items():
            authors[str(client)] = dict(row)
    return RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=DEFAULT_KIND)


def check(name, fn):
    async def run():
        # RoomDoc creates a future at construction, so every case needs a loop.
        result = fn()
        if asyncio.iscoroutine(result):
            await result

    try:
        asyncio.run(run())
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def test_an_agent_can_tell_a_person_from_an_agent():
    doc = make({1: HUMAN, 2: AGENT})
    assert doc.wrote(1)["kind"] == "human"
    assert doc.wrote(2)["kind"] == "agent"


def test_it_can_tell_whose_agent():
    doc = make({2: AGENT})
    assert doc.wrote(2)["owner_mxid"] == "@qingyun:ag2.space"
    assert "owner_mxid" not in doc.wrote(1) if doc.wrote(1) else True


def test_an_unknown_client_is_none_not_a_guess():
    doc = make({1: HUMAN})
    assert doc.wrote(99) is None


def test_a_disputed_id_answers_none_rather_than_picking_one():
    """Two accounts used one client id. Choosing between them would invent the
    answer, and an invented attribution is the thing this whole path avoids."""
    doc = make({7: {"disputed": [HUMAN, AGENT]}})
    assert doc.wrote(7) is None
    assert "disputed" in doc.authors["7"], "the dispute is still visible to a caller"


def test_an_absent_map_is_no_attribution_not_an_error():
    doc = make()
    assert doc.authors == {}
    assert doc.wrote(1) is None


def test_the_client_id_may_be_given_as_int_or_str():
    doc = make({5: HUMAN})
    assert doc.wrote(5) == doc.wrote("5") == HUMAN


def test_reading_authors_writes_nothing_to_the_wire():
    """A reader must not touch the map: writing to it would be claiming."""
    doc = make({1: HUMAN})
    _ = doc.authors, doc.wrote(1)
    assert doc._ws.sent == [], "reading attribution put something on the wire"


def test_json_read_carries_authors_only_when_asked():
    with_authors = json.loads(render("read", text="hi", as_json=True,
                                     authors={"1": HUMAN}))
    assert with_authors["authors"]["1"]["mxid"] == "@qingyun:ag2.space"
    without = json.loads(render("read", text="hi", as_json=True))
    assert "authors" not in without, "attribution must not appear unrequested"


def test_plain_read_names_the_author_above_the_text():
    out = render("read", text="the body", authors={"2": AGENT})
    assert out.index("@mars:ag2space.local") < out.index("the body"), \
        "an agent decides whether to trust the text by who wrote it"
    assert "agent of @qingyun:ag2.space" in out
    # control: no attribution asked for, no attribution printed
    assert render("read", text="the body") == "the body"


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc authors: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc authors: ok")
