#!/usr/bin/env python3
"""Where a drawing lands when an agent draws without looking.

The skill's own example placed every drawing at (0, 0), and `draw` wrote the
coordinates verbatim — so two agents following the skill drew on top of each
other, and the owner saw two complete diagrams overlaid at the same origin.
The failure is silent: both drawings converge correctly, they are just
unreadable together.

Run: python3 tests/room-collab-board-place.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from room_collab_board import PLACE_GAP, bounding_box, place_clear  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def el(ident="a", x=0, y=0, w=100, h=60, **over):
    base = {"id": ident, "type": "rectangle", "x": x, "y": y,
            "width": w, "height": h, "version": 1}
    base.update(over)
    return base


def box(elements):
    return bounding_box(elements)


# --- the defect itself: the same batch drawn twice must not overlap itself

def test_two_draws_of_the_same_batch_do_not_overlap():
    first = [el("p1", 0, 0), el("p2", 120, 0)]
    second = [el("q1", 0, 0), el("q2", 120, 0)]
    placed = place_clear(second, first)
    a, b = box(first), box(placed)
    assert b[1] >= a[3] + PLACE_GAP, f"second batch sits at {b}, first ends at {a[3]}"
    assert [e["x"] for e in placed] == [0, 120], "only y moves; the batch keeps its shape"
    assert placed[1]["y"] - placed[0]["y"] == 0, "relative layout inside the batch is intact"


def test_a_third_drawing_goes_under_the_second_not_the_first():
    first = [el("p1", 0, 0)]
    second = place_clear([el("q1", 0, 0)], first)
    third = place_clear([el("r1", 0, 0)], first + second)
    assert box(third)[1] >= box(second)[3] + PLACE_GAP


# --- a caller that looked is left exactly where it asked to be

def test_clear_coordinates_are_not_touched():
    existing = [el("p1", 0, 0)]
    asked = [el("q1", 0, 500), el("q2", 300, 0)]
    placed = place_clear(asked, existing)
    assert placed == asked, "no overlap, no move"
    assert placed is asked, "not even a copy: nothing happened"


def test_touching_edges_are_adjacent_not_overlapping():
    existing = [el("p1", 0, 0, 100, 60)]
    asked = [el("q1", 0, 60)]  # top edge on the other's bottom edge
    assert place_clear(asked, existing) is asked


# --- an agent re-asserting its OWN elements must not walk down the board

def test_rewriting_your_own_elements_does_not_move_them():
    mine = [el("m1", 0, 0, version=1), el("m2", 120, 0, version=1)]
    again = [el("m1", 0, 0, version=2), el("m2", 120, 0, version=2)]
    assert place_clear(again, mine) is again, "same ids: the old position is not an obstacle"


def test_own_ids_are_excluded_even_when_others_are_present():
    board = [el("m1", 0, 0), el("theirs", 0, 500)]
    again = [el("m1", 0, 0, version=2)]
    assert place_clear(again, board) is again


def test_a_partial_reassert_is_still_measured_against_others():
    # m1 is mine and moves; the stranger at (0,0) is what it must clear.
    board = [el("stranger", 0, 0), el("m1", 0, 300)]
    again = [el("m1", 0, 0, version=2)]
    placed = place_clear(again, board)
    assert placed[0]["y"] >= 60 + PLACE_GAP


# --- deleted elements are not obstacles; invalid ones are neither obstacles nor moved

def test_deleted_elements_do_not_occupy_space():
    existing = [el("gone", 0, 0, isDeleted=True)]
    asked = [el("q1", 0, 0)]
    assert place_clear(asked, existing) is asked


def test_junk_on_the_board_does_not_crash_placement():
    existing = [{"id": "x"}, None, el("p1", 0, 0)]
    placed = place_clear([el("q1", 0, 0)], existing)
    assert placed[0]["y"] >= 60 + PLACE_GAP


def test_junk_in_the_batch_passes_through_unmoved():
    existing = [el("p1", 0, 0)]
    junk = {"id": "bad"}
    placed = place_clear([junk, el("q1", 0, 0)], existing)
    assert placed[0] is junk
    assert placed[1]["y"] >= 60 + PLACE_GAP


# --- the offset is an int, so a caller's y keeps whatever precision it had

def test_the_offset_keeps_the_callers_precision():
    existing = [el("p1", 0, 0, 100, 60)]
    placed = place_clear([el("q1", 0, 0), el("q2", 0, 10.7)], existing)
    assert isinstance(placed[0]["y"], int), type(placed[0]["y"])
    assert placed[1]["y"] == placed[0]["y"] + 10.7, placed[1]["y"]
    assert isinstance(PLACE_GAP, int)


def test_an_unmoved_element_is_the_same_object():
    asked = [el("q1", 0, 0)]
    assert place_clear(asked, []) is asked, "an empty board: nothing to clear"
    assert place_clear([], asked) == [], "an empty batch: nothing to place"


# --- geometry: negative width/height still cover their span

def test_negative_extent_is_measured_as_the_span_it_covers():
    assert box([el("a", 100, 60, -100, -60)]) == (0, 0, 100, 60)
    existing = [el("p1", 100, 60, -100, -60)]
    placed = place_clear([el("q1", 0, 0)], existing)
    assert placed[0]["y"] >= 60 + PLACE_GAP


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab board placement: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab board placement: ok")
