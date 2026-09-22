"""Yjs relative positions for a text, in the units a web client counts.

A relative position names a unit of text by its Yjs ID — (client, clock) —
and follows that unit through everyone's edits, which is what a comment
anchor and a caret need. Yjs numbers units in UTF-16; pycrdt's sticky index
answers in (client, item.clock + byte offset within the item), which agrees
only inside ASCII. So the item lengths are read from the document's own
update — the wire form is UTF-16 — and the sticky index is asked only at each
item's first character, where its answer is exact.
"""
from __future__ import annotations

import struct

from pycrdt import Assoc, Doc, StickyIndex, Text


def units(s: str) -> int:
    """How many UTF-16 units `s` is: what a Yjs index counts."""
    return len(s.encode("utf-16-le")) // 2


class _Reader:
    """lib0's varint/string/any decoding, enough to walk a v1 update."""

    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def var_uint(self) -> int:
        n, shift = 0, 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            if b < 0x80:
                return n
            shift += 7

    def var_int(self) -> int:
        b = self.u8()
        n, neg, shift = b & 0x3F, bool(b & 0x40), 6
        while b & 0x80:
            b = self.u8()
            n |= (b & 0x7F) << shift
            shift += 7
        return -n if neg else n

    def bytes_(self) -> bytes:
        n = self.var_uint()
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def string(self) -> str:
        return self.bytes_().decode("utf-8")

    def fixed(self, fmt: str):
        size = struct.calcsize(fmt)
        v = struct.unpack(fmt, self.d[self.p:self.p + size])[0]
        self.p += size
        return v

    def any(self):
        t = self.u8()
        if t in (127, 126):
            return None
        if t == 125:
            return self.var_int()
        if t == 124:
            return self.fixed(">f")
        if t == 123:
            return self.fixed(">d")
        if t == 122:
            return self.fixed(">q")
        if t == 121:
            return False
        if t == 120:
            return True
        if t == 119:
            return self.string()
        if t == 118:
            return {self.string(): self.any() for _ in range(self.var_uint())}
        if t == 117:
            return [self.any() for _ in range(self.var_uint())]
        if t == 116:
            return self.bytes_()
        raise ValueError(f"unknown lib0 value type {t}")  # pragma: no cover


def blocks(update: bytes) -> list[tuple[int, int, int, str | None]]:
    """Every struct in a Yjs v1 update as (client, clock, length, text) —
    text only for string content, whose length is its UTF-16 unit count."""
    r = _Reader(update)
    out: list[tuple[int, int, int, str | None]] = []
    for _ in range(r.var_uint()):
        count, client, clock = r.var_uint(), r.var_uint(), r.var_uint()
        for _ in range(count):
            info = r.u8()
            kind = info & 0x1F
            text = None
            if kind in (0, 10):  # GC, Skip
                length = r.var_uint()
            else:
                if info & 0x80:
                    r.var_uint(), r.var_uint()  # origin
                if info & 0x40:
                    r.var_uint(), r.var_uint()  # right origin
                if not info & 0xC0:
                    if r.var_uint():
                        r.string()  # a named root type
                    else:
                        r.var_uint(), r.var_uint()
                    if info & 0x20:
                        r.string()  # the key inside a map parent
                length = 1
                if kind == 1:  # Deleted
                    length = r.var_uint()
                elif kind == 2:  # pragma: no cover - JSON content, written by no current Yjs
                    length = r.var_uint()
                    for _ in range(length):
                        r.string()
                elif kind == 3:  # Binary
                    r.bytes_()
                elif kind == 4:  # String
                    text = r.string()
                    length = units(text)
                elif kind == 5:  # Embed
                    r.string()
                elif kind == 6:  # Format
                    r.string(), r.string()
                elif kind == 7:  # Type
                    if r.var_uint() in (3, 5):  # pragma: no cover - XML element/hook names
                        r.string()
                elif kind == 8:  # Any
                    length = r.var_uint()
                    for _ in range(length):
                        r.any()
                elif kind == 9:  # Doc
                    r.string(), r.any()
                else:  # pragma: no cover
                    raise ValueError(f"unknown struct kind {kind}")
            out.append((client, clock, length, text))
            clock += length
    return out


def item_map(doc: Doc, text: Text) -> list[tuple[int, int, str]]:
    """The live items of `text` in document order, as (client, clock, text)."""
    lengths = {(c, k): n for c, k, n, _ in blocks(doc.get_update())}
    items: list[list] = []
    byte_at, need = 0, 0
    for ch in str(text):
        if need <= 0:
            j = text.sticky_index(byte_at, Assoc.AFTER).to_json()["item"]
            key = (j["client"], j["clock"])
            if key not in lengths:
                raise LookupError(f"no item starts at {key}")
            need = lengths[key]
            items.append([key[0], key[1], []])
        items[-1][2].append(ch)
        need -= units(ch)
        byte_at += len(ch.encode("utf-8"))
    return [(c, k, "".join(chars)) for c, k, chars in items]


def relative_position(doc: Doc, text: Text, name: str, index: int) -> dict:
    """The Yjs relative position (as JSON) of UTF-16 offset `index` in `text`,
    right-associated; the end of the text is named by the type, as Yjs does."""
    at = 0
    for client, clock, s in item_map(doc, text):
        n = units(s)
        if index < at + n:
            return {"item": {"client": client, "clock": clock + (index - at)}, "assoc": 0}
        at += n
    if index == at:
        return {"tname": name, "assoc": 0}
    raise IndexError(f"{index} is past the end of the text ({at} units)")


def encode(doc: Doc, text: Text, name: str, index: int) -> bytes:
    """The same position in Yjs's binary form, what a client decodes."""
    return StickyIndex.from_json(relative_position(doc, text, name, index), sequence=text).encode()


def as_awareness_json(position: dict, name: str) -> dict:
    """The four keys a Yjs RelativePosition serializes to: `type`, `tname`,
    `item`, `assoc`, nulls included.

    An awareness cursor is read as raw JSON — `createAbsolutePositionFromRelativePosition`
    takes the object itself, not `createRelativePositionFromJSON` — and its
    `item !== null` test reads a MISSING key as an id, then dereferences it.
    So a key left out is not an absent field here; it is a crash, and the
    caret never draws.
    """
    return {"type": None, "tname": name, "item": position.get("item"),
            "assoc": position.get("assoc", 0)}
