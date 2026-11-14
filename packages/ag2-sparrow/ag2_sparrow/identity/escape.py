"""Component escaping and its canonical-form check — shared by the types and
the derivations, so the grammar has one definition of "what a constructor can
emit" and the parsers accept exactly that."""
from __future__ import annotations

import re

# Reserved separators; escaped in raw components so the grammar is injective.
_RESERVED = "%@#+~:"
_TOKEN = re.compile(r"%[0-9A-F]{2}|[^%]")


def escape_component(raw: str) -> str:
    """Injective: safe chars pass through; everything else (reserved,
    whitespace, path separators, ALL non-ASCII) becomes fixed-width uppercase
    %XX per UTF-8 byte. '%' itself is always escaped, so decoding is
    unambiguous and two distinct inputs can never share an output."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("identity component must be a non-empty string")
    out = []
    for ch in raw:
        if 0x21 <= ord(ch) <= 0x7E and ch not in _RESERVED and ch not in "/\\":
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def is_canonical_component(component: str) -> bool:
    """True only for strings escape_component can emit: every %XX decodes as
    part of valid UTF-8 and re-escaping the decoded text reproduces the input
    byte-for-byte. `%41` (a safe 'A'), `%FF` (not UTF-8) and stray '%' fail."""
    if not isinstance(component, str) or not component:
        return False
    tokens = _TOKEN.findall(component)
    if "".join(tokens) != component:
        return False
    raw = bytearray()
    for tok in tokens:
        raw.extend(bytes.fromhex(tok[1:]) if tok.startswith("%") else tok.encode("utf-8"))
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return escape_component(decoded) == component
