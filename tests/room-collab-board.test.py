#!/usr/bin/env python3
"""The board document's rules — the ones that fail silently if they are wrong.

An element that fails validation is not rejected by the server; it is dropped
by every viewer on read, with no error anywhere. A convergence rule that
disagrees with the web client's loses a shape's newer version instead. Both
look like nothing happening, which is why they are pinned here.

The module under test imports nothing, so this runs wherever CI runs.
Run: python3 tests/room-collab-board.test.py  (exit 0 pass / 1 fail)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "room-collab"))

from room_collab_board import (  # noqa: E402
    BOARD_KIND, ELEMENTS_KEY, FILES_KEY, changed_elements, describe_invalid,
    elements_from_map, is_board_element, is_board_file, is_newer, live_elements,
    sort_elements,
)

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


def test_the_names_match_the_web_client():
    # These three strings ARE the interop contract; a typo opens a document
    # nobody else is in, and it would look like an empty board.
    assert BOARD_KIND == "board"
    assert ELEMENTS_KEY == "elements"
    assert FILES_KEY == "files"


def test_a_well_formed_element_is_accepted():
    assert is_board_element(el())
    assert is_board_element(el(), "a"), "its id must satisfy its own key"


def test_the_id_must_equal_the_key():
    assert not is_board_element(el(id="a"), "b"), "a mismatched key poisons lookup"


def test_the_excluded_types_are_refused():
    # embeddable/iframe/magicframe render third-party content in every viewer.
    for bad in ("embeddable", "iframe", "magicframe"):
        assert not is_board_element(el(type=bad)), f"{bad} must not be a board element"
    assert is_board_element(el(type="freedraw"))


def test_non_finite_geometry_is_refused():
    for field in ("x", "y", "width", "height", "version"):
        assert not is_board_element(el(**{field: float("nan")})), f"NaN {field}"
        assert not is_board_element(el(**{field: float("inf")})), f"inf {field}"
        assert not is_board_element(el(**{field: "3"})), f"string {field}"


def test_a_boolean_is_not_a_number():
    # bool subclasses int in Python; the web client cannot make this mistake.
    assert not is_board_element(el(width=True)), "True is not a width"


def test_optional_fields_are_shape_checked_only_when_present():
    assert is_board_element(el())
    assert is_board_element(el(angle=0.5, versionNonce=7, isDeleted=False, index="a1"))
    assert not is_board_element(el(isDeleted="yes"))
    assert not is_board_element(el(index=3))
    assert not is_board_element(el(angle="x"))


def test_garbage_is_refused_rather_than_crashing():
    for junk in (None, {}, [], "el", 3, {"id": ""}):
        assert not is_board_element(junk), f"{junk!r} must not be an element"


def test_is_newer_prefers_the_higher_version():
    assert is_newer(el(version=2), el(version=1))
    assert not is_newer(el(version=1), el(version=2))


def test_absent_stored_is_always_newer():
    assert is_newer(el(), None)


def test_a_version_tie_breaks_on_nonce_not_on_arrival():
    a = el(version=3, versionNonce=10)
    b = el(version=3, versionNonce=9)
    assert is_newer(a, b)
    assert not is_newer(b, a), "the tie-break must be deterministic, not last-write"


def test_a_missing_nonce_counts_as_zero():
    assert is_newer(el(version=1, versionNonce=1), el(version=1))
    assert not is_newer(el(version=1), el(version=1))


def test_changed_elements_skips_what_is_not_newer():
    stored = {"a": el(version=5)}
    assert changed_elements([el(version=5)], stored.get) == []
    assert len(changed_elements([el(version=6)], stored.get)) == 1


def test_changed_elements_drops_invalid_input():
    assert changed_elements([el(type="iframe"), {}, None], {}.get) == []


def test_a_poisoned_stored_value_is_repaired_not_lost():
    """A stored non-element counts as absent, so a valid copy overwrites it.

    The version must be HIGH for this to discriminate: a poisoned value with no
    version loses to any incoming one anyway, so the repair would look like it
    worked while the rule was gone. One carrying a high version blocks the key
    forever unless it is treated as absent.
    """
    stored = {"a": {"garbage": True, "version": 9999}}
    assert len(changed_elements([el(version=1)], stored.get)) == 1, \
        "a high-versioned poisoned value must not out-rank a valid element"
    # Control: a VALID stored element at that version legitimately wins.
    good = {"a": el(version=9999)}
    assert changed_elements([el(version=1)], good.get) == []


def test_sort_is_by_index_then_id_and_is_stable_across_clients():
    out = sort_elements([el(id="b", index="a2"), el(id="a", index="a1")])
    assert [e["id"] for e in out] == ["a", "b"]
    # Same index: id decides, so two clients cannot order them differently.
    same = sort_elements([el(id="z", index="a1"), el(id="y", index="a1")])
    assert [e["id"] for e in same] == ["y", "z"]


def test_elements_without_an_index_sort_after_those_with_one():
    out = sort_elements([el(id="n"), el(id="i", index="a1")])
    assert [e["id"] for e in out] == ["i", "n"]


def test_elements_from_map_filters_and_orders():
    items = [("a", el(id="a", index="a2")), ("bad", {"nope": 1}), ("b", el(id="b", index="a1"))]
    assert [e["id"] for e in elements_from_map(items)] == ["b", "a"]


def test_live_excludes_deleted_but_elements_keeps_them():
    items = [("a", el(id="a")), ("b", el(id="b", isDeleted=True))]
    assert [e["id"] for e in elements_from_map(items)] == ["a", "b"], \
        "the editor reconciles on isDeleted and must still see it"
    assert [e["id"] for e in live_elements(items)] == ["a"]


def test_a_board_file_must_carry_embedded_image_bytes():
    ok = {"id": "f", "mimeType": "image/png", "created": 1,
          "dataURL": "data:image/png;base64,AAAA"}
    assert is_board_file(ok, "f")
    # A remote URL would make every viewer fetch an attacker's origin.
    assert not is_board_file({**ok, "dataURL": "https://example.com/x.png"})
    # A declared type that disagrees with the payload's.
    assert not is_board_file({**ok, "mimeType": "image/gif"})
    assert not is_board_file({**ok, "mimeType": "text/html"})
    assert not is_board_file({**ok, "created": "now"})


def test_describe_invalid_names_the_reason():
    assert "type" in describe_invalid(el(type="iframe"))
    assert "finite" in describe_invalid(el(width=float("nan")))
    assert "id" in describe_invalid({})
    assert describe_invalid(el()) == "valid", "a control: a good element says so"


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab board rules: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab board rules: ok")
