#!/usr/bin/env python3
"""Yjs relative positions from pycrdt, in the units a web client counts.

The expected IDs below were checked against the client's own `yjs`: the
update applied to a Y.Doc, each encoded position resolved with
createAbsolutePositionFromRelativePosition, index-for-index — including the
case that fools a byte-offset reading (an item whose clock happens to equal
the previous item's clock + its byte length).
"""
import base64
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-collab" / "scripts"))

from pycrdt import Array, Doc, Map, StickyIndex, Text  # noqa: E402

from room_collab_positions import (  # noqa: E402
    as_awareness_json, blocks, encode, item_map, relative_position, units)

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def small():
    doc = Doc()
    text = doc.get("markdown", type=Text)
    text += "héllo"
    text.insert(0, "¡")
    text += "日本"
    text.insert(3, "😀")
    # Clocks: héllo 0-4, ¡ 5, 日本 6-7 (= 0 + héllo's BYTE length: the trap), 😀 8-9.
    return doc, text


def test_an_int_wider_than_six_bits_and_a_foreign_text_are_handled():
    doc = Doc()
    doc.get("m", type=Map).update({"big": 100000, "neg": -300})
    assert blocks(doc.get_update())  # the varint continuation bytes were walked
    other = Doc()
    other_text = other.get("markdown", type=Text)
    other_text += "x"
    try:
        item_map(doc, other_text)  # a text whose items are not in this doc's update
        raise AssertionError("mapped a foreign text")
    except LookupError:
        pass


def test_units_count_utf16_not_chars_or_bytes():
    assert units("héllo") == 5 and units("日本") == 2 and units("😀") == 2 and units("") == 0


def test_blocks_reads_every_struct_kind_pycrdt_writes():
    doc = Doc()
    text = doc.get("t", type=Text)
    text += "ab"
    text.format(0, 1, {"bold": True})
    text.insert_embed(2, {"img": 1})
    m = doc.get("m", type=Map)
    m.update({"i": -5, "f32": 1.5, "f64": 0.1, "s": "x", "b": True, "n": None, "l": [1, "a"],
              "d": {"k": 2}, "by": b"\x01\x02", "sub": Map({"z": 1}), "arr": Array([1]), "doc": Doc()})
    got = blocks(doc.get_update())
    strings = [(k, n, t) for _c, k, n, t in got if t is not None]
    assert ("a" in "".join(t for _k, _n, t in strings)), strings
    assert all(n >= 1 for _c, _k, n, _t in got), got
    # Clocks are contiguous per client: every struct's length was walked exactly.
    by_client = {}
    for c, k, n, _t in got:
        by_client.setdefault(c, []).append((k, n))
    for runs in by_client.values():
        runs.sort()
        for (k1, n1), (k2, _n2) in zip(runs, runs[1:]):
            assert k1 + n1 == k2, runs
    del text[0:1]  # a deleted, collected unit becomes a GC struct that still holds its clock
    got2 = blocks(doc.get_update())
    assert sum(n for _c, _k, n, _t in got2) == sum(n for _c, _k, n, _t in got), (got, got2)


def test_structs_only_a_web_peer_writes_are_walked_too():
    # One client (7) with a collected block of 3, a skip of 2, and a binary
    # value under map "m" key "k"; then an empty delete set.
    update = (b"\x01" b"\x03\x07\x00"
              b"\x00\x03"
              b"\x0a\x02"
              b"\x23" b"\x01" b"\x01m" b"\x01k" b"\x02\xaa\xbb"
              b"\x00")
    assert blocks(update) == [(7, 0, 3, None), (7, 3, 2, None), (7, 5, 1, None)], blocks(update)


def test_a_multibyte_string_item_is_measured_in_utf16_units():
    doc = Doc()
    text = doc.get("t", type=Text)
    text += "héllo 日本 😀"
    [(c, k, n, t)] = [b for b in blocks(doc.get_update()) if b[3] is not None]
    assert (k, n, t) == (0, 11, "héllo 日本 😀"), (k, n, t)


def test_item_map_is_the_document_order_with_real_item_boundaries():
    doc, text = small()
    got = [(k, s) for _c, k, s in item_map(doc, text)]
    assert got == [(5, "¡"), (0, "h"), (8, "😀"), (1, "éllo"), (6, "日本")], got


def test_positions_are_the_ids_yjs_resolves_to_the_same_index():
    doc, text = small()
    client = doc.client_id
    s = str(text)
    assert s == "¡h😀éllo日本" and units(s) == 10
    want = {0: 5, 1: 0, 2: 8, 3: 9, 4: 1, 5: 2, 6: 3, 7: 4, 8: 6, 9: 7}
    for index, clock in want.items():
        got = relative_position(doc, text, "markdown", index)
        assert got == {"item": {"client": client, "clock": clock}, "assoc": 0}, (index, got)
    assert relative_position(doc, text, "markdown", 10) == {"tname": "markdown", "assoc": 0}
    try:
        relative_position(doc, text, "markdown", 11)
        raise AssertionError("accepted a position past the end")
    except IndexError:
        pass


def test_a_byte_offset_reading_would_have_been_wrong_here():
    # pycrdt's own sticky index at byte 9 ("l") reports clock 3: one unit late.
    from pycrdt import Assoc
    doc, text = small()
    assert text.sticky_index(9, Assoc.AFTER).to_json()["item"]["clock"] == 3
    assert relative_position(doc, text, "markdown", 5)["item"]["clock"] == 2


def test_encode_is_the_binary_yjs_form_of_the_same_position():
    doc, text = small()
    raw = encode(doc, text, "markdown", 8)
    back = StickyIndex.decode(raw, sequence=text).to_json()
    assert back == relative_position(doc, text, "markdown", 8), back
    assert base64.b64encode(encode(doc, text, "markdown", 10)).decode() == base64.b64encode(
        StickyIndex.from_json({"tname": "markdown", "assoc": 0}, sequence=text).encode()).decode()


def test_an_awareness_cursor_carries_all_four_keys_a_yjs_position_serializes_to():
    # A peer reads a MISSING `item` as an id (undefined !== null) and
    # dereferences it, dropping the caret. Measured against the client's yjs.
    doc, text = small()
    inside = as_awareness_json(relative_position(doc, text, "markdown", 4), "markdown")
    end = as_awareness_json(relative_position(doc, text, "markdown", 10), "markdown")
    for got in (inside, end):
        assert set(got) == {"type", "tname", "item", "assoc"}, got
        assert got["type"] is None and got["tname"] == "markdown" and got["assoc"] == 0, got
    assert inside["item"] == {"client": doc.client_id, "clock": 1}, inside
    assert end["item"] is None, "the end of the text is the one position with no item"


def test_a_peers_items_and_deletions_are_placed_too():
    doc = Doc()
    text = doc.get("markdown", type=Text)
    text += "one 日本 two"
    peer = Doc()
    peer.apply_update(doc.get_update())
    ptext = peer.get("markdown", type=Text)
    ptext.insert(0, "¡")
    doc.apply_update(peer.get_update(doc.get_state()))
    del text[6:12]  # the bytes of "日本": gone from the text, still in the clocks
    s = str(text)
    assert s == "¡one  two", s
    items = item_map(doc, text)
    assert [x for _c, _k, x in items] == ["¡", "one ", " two"], items
    assert items[0][0] == peer.client_id and items[1][0] == doc.client_id
    # After the deletion, the unit at index 5 is " " of " two", whose clock skips the deleted two.
    assert relative_position(doc, text, "markdown", 5) == {"item": {"client": doc.client_id, "clock": 6}, "assoc": 0}


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab positions: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab positions: ok")
