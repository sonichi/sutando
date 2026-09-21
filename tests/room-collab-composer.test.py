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
    ComposerError, EMAIL, LINKEDIN_POST, SCHEMA, X_POST, build, counted_length,
    over_limit, readable,
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


# --- what a reader does with a draft it cannot draw

def test_a_known_draft_reads_as_itself():
    got = readable(build(EMAIL, {"subject": "s", "to": ["a@b.c"]}))
    assert got["type"] == EMAIL and got["schema"] == SCHEMA and got["plain"] is False
    assert got["fields"] == {"subject": "s", "to": ["a@b.c"]}, got
    assert "why" not in got


def test_skew_in_either_direction_degrades_to_plain_and_never_raises():
    # The writer is newer: a type this reader has no renderer for.
    newer_type = readable({"type": "bluesky_post", "schema": 1, "text": "hi"})
    assert newer_type["plain"] and "no renderer for type" in newer_type["why"]
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
