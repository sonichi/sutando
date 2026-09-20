#!/usr/bin/env python3
"""A minimal element written by an agent must reach the editor complete.

The web panel hands the board map to Excalidraw as-is, and Excalidraw's
selection handler reads `groupIds` (and more) on EVERY element, not just the
one clicked. One element missing the field, anywhere on the board, and
nothing on that board can be selected again. Thirteen such elements were on
a live board tonight, all written by the skill's own minimal example.

The fill mirrors what Excalidraw's restoreElement() does, so an element that
went through this is what the editor would have made of it. Pure: no pycrdt.
Run: python3 tests/room-doc-board-complete.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

from room_doc_board import (ELEMENT_DEFAULTS, complete_element,  # noqa: E402
                            is_board_element)

FAILS = []

# The fields restoreElement() fills, read from the bundled editor (0.18.1).
EDITOR_READS = ("angle", "strokeColor", "backgroundColor", "fillStyle", "strokeWidth",
                "strokeStyle", "roughness", "opacity", "groupIds", "frameId", "roundness",
                "seed", "versionNonce", "isDeleted", "boundElements", "updated", "link",
                "locked")


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def minimal(ident="r1", type_="rectangle"):
    return {"id": ident, "type": type_, "x": 0, "y": 0, "width": 100, "height": 60, "version": 1}


def test_a_minimal_element_gains_every_field_the_editor_reads():
    out = complete_element(minimal(), now_ms=1_000)
    missing = [k for k in EDITOR_READS if k not in out]
    assert not missing, f"still missing after completion: {missing}"
    assert out["groupIds"] == [] and out["isDeleted"] is False, out
    assert is_board_element(out, "r1"), "completion must keep it valid"


def test_what_the_caller_set_is_never_changed():
    given = {**minimal(), "strokeColor": "#ff0000", "groupIds": ["g1"], "opacity": 40,
             "seed": 7, "isDeleted": True}
    out = complete_element(given, now_ms=1_000)
    for k, v in given.items():
        assert out[k] == v, f"{k} was changed from {v!r} to {out[k]!r}"


def test_a_complete_element_passes_through_equal_to_itself():
    full = complete_element(minimal(), now_ms=1_000)
    assert complete_element(full, now_ms=2_000) == full, "a second pass must be a no-op"


def test_each_call_gets_its_own_group_list():
    a, b = complete_element(minimal("a")), complete_element(minimal("b"))
    a["groupIds"].append("x")
    assert b["groupIds"] == [], "the default list was shared between elements"


def test_text_gets_what_the_editor_needs_to_lay_it_out():
    out = complete_element({**minimal("t", "text"), "text": "hi"}, now_ms=1)
    for k in ("fontSize", "fontFamily", "textAlign", "verticalAlign", "lineHeight",
              "containerId", "originalText"):
        assert k in out, k
    assert out["originalText"] == "hi"
    assert "fontSize" not in complete_element(minimal(), now_ms=1), \
        "text fields must not leak onto a rectangle"


def test_an_arrow_gets_points_and_an_arrowhead():
    out = complete_element(minimal("a", "arrow"), now_ms=1)
    assert out["points"] == [[0, 0], [100, 60]], out["points"]
    assert out["endArrowhead"] == "arrow" and out["startArrowhead"] is None
    line = complete_element(minimal("l", "line"), now_ms=1)
    assert line["endArrowhead"] is None, "a line has no arrowhead"
    explicit = complete_element({**minimal("a2", "arrow"), "endArrowhead": None}, now_ms=1)
    assert explicit["endArrowhead"] is None, "an explicit None must survive"


def test_re_asserting_a_minimal_element_keeps_the_stored_identity():
    """Without this, every re-assert minted a new versionNonce and the
    version tie-break flipped a coin on whether to rewrite the element."""
    stored = complete_element(minimal(), now_ms=1_000)
    again = complete_element(minimal(), base=stored, now_ms=5_000)
    assert again["seed"] == stored["seed"] and again["versionNonce"] == stored["versionNonce"]
    assert again["updated"] == 5_000, "updated is THIS write's time, not the stored one's"
    fresh = complete_element(minimal("other"), now_ms=5_000)
    assert fresh["updated"] == 5_000, "with no stored copy the defaults apply"


def test_a_freedraw_gets_points_before_the_editor_measures_it():
    """restoreElements calls isInvisiblySmallElement first, which reads
    points.length on linear AND freedraw elements; one freedraw without
    points throws for the whole batch."""
    out = complete_element(minimal("f", "freedraw"), now_ms=1)
    assert out["points"] == [[0, 0]] and out["pressures"] == [] and out["simulatePressure"] is True
    assert out["lastCommittedPoint"] is None
    given = complete_element({**minimal("f2", "freedraw"), "points": [[0, 0], [5, 5]]}, now_ms=1)
    assert given["points"] == [[0, 0], [5, 5]]


def test_bound_elements_default_matches_the_editors_restore():
    """restoreElement fills `boundElements ?? []`; a None here would make the
    editor's own restore a non-identity on our output."""
    assert complete_element(minimal(), now_ms=1)["boundElements"] == []
    assert complete_element({**minimal(), "boundElements": None}, now_ms=1)["boundElements"] == []


def test_the_default_set_is_the_editors_not_a_guess():
    """Pin the values, so a drift from Excalidraw's defaults is a diff to read."""
    assert ELEMENT_DEFAULTS["strokeColor"] == "#1e1e1e"
    assert ELEMENT_DEFAULTS["backgroundColor"] == "transparent"
    assert ELEMENT_DEFAULTS["opacity"] == 100 and ELEMENT_DEFAULTS["strokeWidth"] == 2


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc board complete: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc board complete: ok")
