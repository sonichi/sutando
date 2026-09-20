#!/usr/bin/env python3
"""What the CLI prints for a board.

`render()` is pure so this needs no pycrdt and no socket. The property that
matters: a board read must never fall through to the text shape, where an
empty string means "empty board" and "this is not a text document" alike.
Run: python3 tests/room-doc-board-cli.test.py  (exit 0 pass / 1 fail)
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

from room_doc import build_parser, parse_elements, render  # noqa: E402
from room_doc_protocol import RoomDocError  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def el(**over):
    base = {"id": "a", "type": "rectangle", "x": 0, "y": 0,
            "width": 10, "height": 10, "version": 1}
    base.update(over)
    return base


def test_an_empty_board_says_so_rather_than_printing_nothing():
    out = render("read", elements=[])
    assert out.strip(), "an empty line reads as a broken command"
    assert "0 elements" in out, out


def test_a_board_read_lists_its_elements():
    out = render("read", elements=[el(id="r1", type="rectangle", index="a1")])
    assert "r1" in out and "rectangle" in out, out


def test_a_deleted_element_is_marked_in_the_listing():
    out = render("read", elements=[el(isDeleted=True)])
    assert "deleted" in out, out


def test_board_json_carries_elements_not_a_chars_count():
    payload = json.loads(render("read", elements=[el()], as_json=True))
    assert payload["count"] == 1
    assert payload["elements"][0]["id"] == "a"
    assert "chars" not in payload, "the text shape must not leak into a board read"


def test_the_text_read_is_unchanged_when_no_elements_are_given():
    """Control: passing no elements still renders the markdown document."""
    assert render("read", text="hello") == "hello"
    payload = json.loads(render("read", text="hi", as_json=True))
    assert payload["chars"] == 2 and payload["text"] == "hi"


def test_draw_reports_how_many_were_written():
    payload = json.loads(render("draw", elements=[el()], written=1))
    assert payload == {"ok": True, "written": 1, "count": 1}


def test_draw_reports_zero_when_nothing_was_newer():
    payload = json.loads(render("draw", elements=[el()], written=0))
    assert payload["written"] == 0, "a no-op write must not claim it wrote"


def test_the_parser_exposes_the_board_commands():
    p = build_parser()
    for argv in (["--kind", "board", "read", "!r:s"],
                 ["--kind", "board", "draw", "!r:s", "[]"],
                 ["--kind", "board", "erase", "!r:s", "id1"]):
        args = p.parse_args(argv)
        assert args.kind == "board", argv
    assert p.parse_args(["read", "!r:s"]).kind == "markdown", "default stays markdown"


def test_malformed_draw_json_is_a_refusal_not_a_traceback():
    """Every other error path here prints `room-doc: ...`; this one used to
    raise JSONDecodeError straight through argparse."""
    for bad in ("not json", "", "{"):
        try:
            parse_elements(bad)
        except RoomDocError as exc:
            assert "JSON" in str(exc), exc
        else:
            raise AssertionError(f"{bad!r} should not have parsed")


def test_a_bare_object_is_refused_because_one_element_still_needs_a_list():
    try:
        parse_elements('{"id":"a"}')
    except RoomDocError as exc:
        assert "ARRAY" in str(exc), exc
    else:
        raise AssertionError("a bare object should be refused")


def test_valid_json_still_parses():
    assert parse_elements('[{"id":"a"}]') == [{"id": "a"}]
    assert parse_elements("[]") == []


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc board CLI: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc board CLI: ok")
