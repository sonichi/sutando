#!/usr/bin/env python3
"""The agent's write path into a composer: many posts, one text each.

The rules live in `room_composer` and are tested there. This is the half that
touches the CRDT: that a post's prose lands in its own text root, that the
caret says which post it is in, and that two agents filing at the same moment
both keep their post — the failure a collaborative surface exists to prevent.

Run: python3 tests/room-collab-composer-client.test.py  (exit 0 pass / 1 fail)
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "room-collab"))

from pycrdt import Awareness, Doc, Map, Text  # noqa: E402

from room_collab_client import RoomDoc  # noqa: E402
from room_collab_protocol import (  # noqa: E402
    DEFAULT_KIND, DEFAULT_TEXT_NAME, RoomDocError,
)
from room_composer import (  # noqa: E402
    COMPOSER_KIND, DRAFT, EMAIL, POSTS_KEY, SENT, X_POST,
)

FAILS = []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make(kind=COMPOSER_KIND):
    doc = Doc()
    doc.get(POSTS_KEY, type=Map)
    return doc, RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=kind)


def refuses(fn, *words):
    try:
        fn()
    except RoomDocError as e:
        for w in words:
            assert w in str(e), (w, str(e))
        return
    raise AssertionError("expected RoomDocError")


async def refuses_await(coro, *words):
    try:
        await coro
    except RoomDocError as e:
        for w in words:
            assert w in str(e), (w, str(e))
        return
    raise AssertionError("expected RoomDocError")


def cursor_root(room):
    state = room._awareness.states.get(room._awareness.client_id) or {}
    return (state.get("cursor") or {}).get("anchor", {}).get("tname")


# --- a composer has no default text

async def test_a_write_with_no_post_open_is_refused_and_says_how_to_open_one():
    _, room = make()
    for what in (lambda: room.text, lambda: room.anchor(0, 0)):
        refuses(what, "until a post is open", "open_post")
    await refuses_await(room.append("hi"), "until a post is open")
    assert room.post is None


async def test_the_markdown_document_is_untouched_by_any_of_this():
    doc = Doc()
    doc.get(DEFAULT_TEXT_NAME, type=Text)
    room = RoomDoc(FakeWS(), doc, Awareness(doc), DEFAULT_TEXT_NAME, kind=DEFAULT_KIND)
    await room.append("still a text")
    assert room.text == "still a text" and room.post is None
    refuses(lambda: room.posts(), "posts live on the composer", COMPOSER_KIND)


# --- one post, one text root

async def test_a_posts_prose_lands_in_its_own_root_and_the_caret_names_it():
    doc, room = make()
    row = await room.add_post("a1", X_POST, created=1000)
    assert row == {"type": X_POST, "schema": 1, "created": 1000}
    assert room.post == "a1"
    await room.append("first draft")
    assert str(doc.get("post:a1", type=Text)) == "first draft"
    assert str(doc.get(DEFAULT_TEXT_NAME, type=Text)) == "", "the markdown root is not a fallback"
    assert cursor_root(room) == "post:a1", "the caret says which post the agent is in"


async def test_opening_another_post_moves_the_caret_with_it():
    doc, room = make()
    await room.add_post("a1", X_POST, created=1)
    await room.append("one")
    await room.add_post("b2", X_POST, created=2)
    await room.append("two")
    assert cursor_root(room) == "post:b2"
    assert room.open_post("a1") == "post:a1"
    assert room.text == "one" and room.post == "a1"
    await room.append("!")
    assert str(doc.get("post:a1", type=Text)) == "one!"
    assert str(doc.get("post:b2", type=Text)) == "two", "the other post is not touched"
    assert cursor_root(room) == "post:a1"


async def test_a_post_must_have_a_row_before_it_can_be_opened():
    _, room = make()
    refuses(lambda: room.open_post("ghost"), "no post 'ghost'", "invisible")
    refuses(lambda: room.open_post("NOT AN ID"), "not a usable post id")


async def test_a_row_a_renderer_could_not_draw_is_refused_here_not_stored():
    doc, room = make()
    for bad, word in ((lambda: room.add_post("e1", "bluesky_post"), "unknown draft type"),
                      (lambda: room.add_post("e1", EMAIL, {"subject": "s"}), "needs 'to'"),
                      (lambda: room.add_post("e1", X_POST, {"text": "hi"}), "no field 'text'")):
        try:
            await bad()
            raise AssertionError("expected a refusal")
        except RoomDocError as e:
            assert word in str(e), (word, str(e))
    assert room.posts() == [] and dict(doc.get(POSTS_KEY, type=Map).to_py()) == {}


async def test_filing_a_post_twice_is_refused_rather_than_replacing_it():
    _, room = make()
    await room.add_post("a1", X_POST, created=1)
    await room.append("someone is writing here")
    await refuses_await(room.add_post("a1", X_POST, created=2), "already on this composer")
    assert room.open_post("a1") and room.text == "someone is writing here"


# --- the merge claim this shape exists for

async def test_two_agents_filing_at_the_same_moment_both_keep_their_post():
    doc, room = make()
    await room.add_post("a1", X_POST, created=1)
    await room.append("mine")
    # A second agent starts from the same state and files its own, unaware.
    peer = Doc()
    peer.apply_update(doc.get_update())
    peer.get(POSTS_KEY, type=Map)["b2"] = {"type": X_POST, "schema": 1, "created": 1}
    peer.get("post:b2", type=Text).__iadd__("theirs")
    doc.apply_update(peer.get_update())
    assert [e["id"] for e in room.posts()] == ["a1", "b2"], "neither post is lost"
    assert room.open_post("b2") and room.text == "theirs"
    assert room.open_post("a1") and room.text == "mine"


async def test_two_agents_writing_different_posts_do_not_touch_each_other():
    doc, room = make()
    await room.add_post("a1", X_POST, created=1)
    await room.append("a paragraph the agent wrote")
    peer = Doc()
    peer.apply_update(doc.get_update())
    peer.get("post:a1", type=Text).insert(0, "a line the person typed\n")
    doc.apply_update(peer.get_update())
    assert room.text == "a line the person typed\na paragraph the agent wrote", \
        "same post: both survive, which is why prose is a text and not a field"


# --- where a post is in its life

async def test_status_moves_a_post_out_of_the_feed_and_keeps_its_words():
    doc, room = make()
    await room.add_post("a1", X_POST, created=1)
    await room.append("what went out")
    assert [(e["id"], e["status"], e["archived"]) for e in room.posts()] == [("a1", DRAFT, False)]
    row = await room.set_status("a1", SENT, 120)
    assert row == {"type": X_POST, "schema": 1, "created": 1, "status": SENT, "status_at": 120}
    entry = room.posts()[0]
    assert (entry["status"], entry["archived"], entry["status_at"]) == (SENT, True, 120)
    assert room.open_post("a1") and room.text == "what went out", "archived is a move, not a delete"
    await refuses_await(room.set_status("nope", SENT), "no post 'nope'")


async def test_the_feed_tolerates_what_another_writer_put_there():
    doc, room = make()
    posts = doc.get(POSTS_KEY, type=Map)
    posts["good"] = {"type": X_POST, "schema": 1, "created": 2}
    posts["future"] = {"type": "bluesky_post", "schema": 9}
    posts["not an id"] = {"type": X_POST, "schema": 1}
    got = room.posts()
    assert [e["id"] for e in got] == ["future", "not an id", "good"], got
    assert got[0]["plain"] and "type 'bluesky_post' is not one this reader knows" in got[0]["why"]
    assert got[1]["plain"] and "not one this reader knows" in got[1]["why"], \
        "a row this reader cannot check is shown saying so, never dropped"
    assert got[2]["root"] == "post:good"
    # ...but it is not openable: this client can only address ids it can check.
    refuses(lambda: room.open_post("not an id"), "not a usable post id")


async def main():
    for name, fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
        try:
            await fn()
        except AssertionError as e:
            FAILS.append(f"{name}: {e}")
        except Exception as e:  # noqa: BLE001
            FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")
    if FAILS:
        print("room-collab composer client: FAIL")
        for f in FAILS:
            print("  -", f)
        return 1
    print("room-collab composer client: ok")
    return 0


sys.exit(asyncio.run(main()))
