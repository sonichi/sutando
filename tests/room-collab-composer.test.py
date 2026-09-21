#!/usr/bin/env python3
"""A draft's typed half: what the map carries, and what the destination counts.

The prose is not here — it is the surface's text, so that a person and an
agent editing the same paragraph merge instead of overwriting. These are the
rules around it: which fields a type takes, what a reader does with a draft it
cannot draw, and how long the destination thinks the prose is.

Run: python3 tests/room-collab-composer.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from room_composer import (  # noqa: E402
    ComposerError, DRAFT, DROPPED, EMAIL, LINKEDIN_POST, SCHEMA, SENT, X_POST, build,
    counted_length, feed, mark, over_limit, readable, root_for, status_of,
)

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def _refuses(fn, *words):
    try:
        fn()
    except ComposerError as e:
        for w in words:
            assert w in str(e), (w, str(e))
        return
    raise AssertionError("expected ComposerError")


# --- what the map carries

def test_a_post_has_no_fields_of_its_own_because_its_content_is_the_prose():
    assert build(X_POST) == {"type": X_POST, "schema": SCHEMA}
    assert build(LINKEDIN_POST) == {"type": LINKEDIN_POST, "schema": SCHEMA}
    _refuses(lambda: build(X_POST, {"text": "hi"}), "x_post has no field 'text'", "content is the text")


def test_an_email_needs_a_subject_and_a_recipient():
    got = build(EMAIL, {"subject": "hi", "to": ["a@b.c"], "cc": ["d@e.f"]})
    assert got == {"type": EMAIL, "schema": SCHEMA, "subject": "hi",
                   "to": ["a@b.c"], "cc": ["d@e.f"]}, got
    _refuses(lambda: build(EMAIL, {"to": ["a@b.c"]}), "needs 'subject'", "writer's bug")
    _refuses(lambda: build(EMAIL, {"subject": "hi"}), "needs 'to'")
    _refuses(lambda: build(EMAIL, {"subject": "  ", "to": ["a@b.c"]}), "needs 'subject'")
    _refuses(lambda: build(EMAIL, {"subject": "hi", "to": []}), "needs 'to'")
    _refuses(lambda: build(EMAIL, {"subject": "hi", "to": ["a@b.c"], "bcc": ["x@y.z"]}),
             "no field 'bcc'", "subject, to, cc")


def test_an_address_is_one_plain_string_per_entry():
    got = build(EMAIL, {"subject": "s", "to": "  solo@b.c  ", "cc": ["a@b.c", "  ", "Name <d@e.f>"]})
    assert got["to"] == ["solo@b.c"], "a bare string is one address, trimmed"
    assert got["cc"] == ["a@b.c", "Name <d@e.f>"], "blanks dropped, display names kept inside the string"


def test_the_type_set_is_closed_because_the_renderers_are_ours():
    _refuses(lambda: build("bluesky_post"), "unknown draft type", "x_post, linkedin_post, email")


# --- a surface holds a feed of posts, each with its own prose

def test_a_post_id_names_a_text_root_so_it_stays_boring():
    assert root_for("a1") == "post:a1" and root_for("3") == "post:3"
    for bad in ("", None, "Post1", "post 1", "post/1", "-lead", "x" * 33):
        _refuses(lambda b=bad: root_for(b), "not a usable post id")


def test_the_feed_is_ordered_by_created_then_id_so_every_client_agrees():
    stored = {
        "2": build(X_POST, created=1002),
        "1": build(X_POST, created=1000),
        # Same minute as "2": an agent filing a batch. The id breaks the tie,
        # or two clients would order these by map iteration and disagree.
        "3": build(EMAIL, {"subject": "s", "to": ["a@b.c"]}, created=1002),
        "9": {"type": X_POST, "schema": SCHEMA},            # no created: sorts to the top, stably
    }
    got = feed(stored)
    assert [e["id"] for e in got] == ["9", "1", "2", "3"], got
    assert [e["root"] for e in got] == ["post:9", "post:1", "post:2", "post:3"]
    assert got[0]["created"] is None and got[1]["created"] == 1000
    assert [e["type"] for e in got] == [X_POST, X_POST, X_POST, EMAIL]
    assert got[3]["fields"] == {"subject": "s", "to": ["a@b.c"]}, "created is not left in the fields"
    # Reversing the input cannot change the answer: the order is in the data.
    assert [e["id"] for e in feed(dict(reversed(list(stored.items()))))] == ["9", "1", "2", "3"]


def test_a_feed_tolerates_what_another_writer_may_have_put_there():
    assert feed(None) == [] and feed("nonsense") == [] and feed({}) == []
    junk = {"1": None, "2": "not a row", "3": {"type": "future", "schema": 99},
            "4": {"type": X_POST, "schema": SCHEMA, "created": True},
            "5": {"type": X_POST, "schema": SCHEMA, "created": 7.5}}
    got = feed(junk)
    assert [e["id"] for e in got] == ["1", "2", "3", "4", "5"], got
    assert all(e["created"] is None for e in got), "a bool or a fraction is not a minute"
    assert got[2]["plain"] and "is not one this reader knows" in got[2]["why"]


def test_an_id_this_reader_cannot_check_still_reaches_the_feed():
    # Dropping a row whose id shape is newer loses a post while reporting a
    # healthy shorter feed — invisible, and permanent for that reader.
    stored = {"p1": build(X_POST, created=1), "P3": build(X_POST, created=2),
              "post_2": build(X_POST, created=3), "x" * 33: build(X_POST, created=4),
              "p4": {"type": "future", "schema": SCHEMA}}
    got = feed(stored)
    assert len(got) == len(stored), f"every stored post reaches the feed: {got}"
    by_id = {e["id"]: e for e in got}
    for bad in ("P3", "post_2", "x" * 33):
        assert by_id[bad]["plain"] and "is not one this reader knows" in by_id[bad]["why"], bad
        assert by_id[bad]["root"] == f"post:{bad}", "its prose is still addressable"
    assert by_id["p1"]["plain"] is False and "why" not in by_id["p1"]
    # Two reasons at once read as two reasons, not as one overwriting the other.
    both = feed({"Bad-Id": {"type": "future", "schema": 99}})[0]
    assert both["why"].count("is not one this reader knows") == 2, both
    assert both["why_about"] == [{"kind": "post-id", "subject": "Bad-Id"},
                                 {"kind": "type", "subject": "future"}], both


def test_the_reasons_come_back_as_data_so_a_client_need_not_parse_the_sentence():
    # The subject is the join key a renderer compares its own reasons against;
    # parsing it back out of the prose is what this exists to stop.
    assert readable({"type": "future", "schema": SCHEMA})["why_about"] == \
        [{"kind": "type", "subject": "future"}]
    assert readable({"type": X_POST, "schema": 9})["why_about"] == \
        [{"kind": "schema", "subject": 9}]
    assert readable({})["why_about"] == [{"kind": "type", "subject": None}], \
        "a reason about an absent type still names what it is about"
    # Present exactly when `why` is, so one absence cannot mean two things.
    for row in (build(X_POST), build(EMAIL, {"subject": "s", "to": ["a@b.c"]})):
        got = readable(row)
        assert "why" not in got and "why_about" not in got, got
    for entry in feed({"p1": build(X_POST), "p2": {"type": "future", "schema": 1}}):
        assert ("why" in entry) == ("why_about" in entry), entry
    # The sentence stays the display form: it is not rebuilt from the data.
    assert "showing the fields as text" in readable({"type": "future", "schema": 1})["why"]


def test_a_post_carries_where_it_is_in_its_life_and_an_unknown_one_is_a_draft():
    assert mark(SENT, 120) == {"status": SENT, "status_at": 120}
    assert mark(DROPPED) == {"status": DROPPED}, "a mark without a time is still a mark"
    _refuses(lambda: mark("posted"), "unknown status", "draft, sent, dropped")
    # The composer does not send, so nothing marks a post finished on its own.
    # An absent or unrecognised status must read as still needing attention.
    for row in (None, {}, {"status": None}, {"status": "weird"}, {"status": 7}, "junk"):
        assert status_of(row) == DRAFT, row


def test_archived_posts_are_named_as_such_and_never_inferred():
    rows = {
        "1": build(X_POST, created=100),
        "2": {**build(X_POST, created=101), **mark(SENT, 120)},
        "3": {**build(EMAIL, {"subject": "s", "to": ["a@b.c"]}, created=102), **mark(DROPPED, 130)},
        "4": {**build(X_POST, created=103), "status": "weird", "status_at": True},
    }
    got = {e["id"]: e for e in feed(rows)}
    assert [got[i]["status"] for i in ("1", "2", "3", "4")] == [DRAFT, SENT, DROPPED, DRAFT]
    assert [got[i]["archived"] for i in ("1", "2", "3", "4")] == [False, True, True, False]
    assert got["2"]["status_at"] == 120 and got["1"]["status_at"] is None
    assert got["4"]["status_at"] is None, "a bool is not a minute"
    assert got["4"]["status_said"] == "weird", \
        "reading it as a draft must not also erase the word a newer writer used"
    assert "status_said" not in got["1"] and "status_said" not in got["2"], \
        "only a status this reader had to reinterpret is worth reporting"
    for entry in got.values():
        assert "status" not in entry["fields"] and "status_at" not in entry["fields"], \
            "the lifecycle is read from its own keys, not left among the renderable fields"
    assert got["3"]["fields"] == {"subject": "s", "to": ["a@b.c"]}, "an archived post keeps its content"


# --- what a reader does with a draft it cannot draw

def test_a_known_draft_reads_as_itself():
    got = readable(build(EMAIL, {"subject": "s", "to": ["a@b.c"]}))
    assert got["type"] == EMAIL and got["schema"] == SCHEMA and got["plain"] is False
    assert got["fields"] == {"subject": "s", "to": ["a@b.c"]}, got
    assert "why" not in got


def test_skew_in_either_direction_degrades_to_plain_and_never_raises():
    newer_type = readable({"type": "bluesky_post", "schema": 1, "text": "hi"})
    # The reason names what this reader could not check — never "no renderer",
    # which is the client's judgement and would mask its own, truer sentence.
    assert newer_type["plain"] and "type 'bluesky_post' is not one this reader knows" in newer_type["why"]
    assert "renderer" not in newer_type["why"], "this module has no renderers to speak for"
    assert newer_type["fields"] == {"text": "hi"}, "the fields still come back, to show as text"
    # The writer is newer: a schema this reader does not know.
    newer_schema = readable({"type": X_POST, "schema": 9})
    assert newer_schema["plain"] and "schema 9" in newer_schema["why"]
    # The writer is OLDER, or the stored value is junk: same treatment, no raise.
    for junk in (None, "not a dict", 7, {}, {"type": None, "schema": None}, {"schema": SCHEMA}):
        out = readable(junk)
        assert out["plain"] is True and isinstance(out["fields"], dict), junk
    # A CRDT map hands numbers back as floats; a whole one is the integer it was.
    assert readable({"type": X_POST, "schema": 1.0})["plain"] is False


# --- what the destination counts

def test_x_weighs_cjk_and_emoji_double_so_a_chinese_post_ends_at_140():
    assert counted_length(X_POST, "hello world") == 11
    assert counted_length(X_POST, "héllo") == 5, "Latin-1 accents are in the light range"
    assert counted_length(X_POST, "中文测试") == 8, "four CJK characters cost eight"
    assert counted_length(X_POST, "😀") == 2, "an emoji is outside the light ranges"
    assert counted_length(X_POST, "a中") == 3
    assert over_limit(X_POST, "中" * 140) == 0, "140 Chinese characters is exactly the limit"
    assert over_limit(X_POST, "中" * 141) == 2, "the 141st costs two, not one"
    assert over_limit(X_POST, "a" * 280) == 0 and over_limit(X_POST, "a" * 281) == 1


def test_linkedin_counts_plainly_and_an_unlimited_type_never_overflows():
    assert counted_length(LINKEDIN_POST, "中文") == 2, "no weighting outside X"
    assert counted_length(LINKEDIN_POST, "😀") == 2, "but an astral character is two UTF-16 units"
    assert over_limit(LINKEDIN_POST, "中" * 3000) == 0
    assert over_limit(LINKEDIN_POST, "a" * 3001) == 1
    assert over_limit(EMAIL, "a" * 100000) == 0, "an email has no length the composer enforces"
    assert over_limit(X_POST, "") == 0 and counted_length(X_POST, None) == 0


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab composer: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab composer: ok")
