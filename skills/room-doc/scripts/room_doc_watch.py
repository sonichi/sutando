"""What changed in a text document, and which of it is addressed to me.

An agent holding a document open receives every remote edit as it lands, but
an edit is a delta of characters, not a message. These rules turn two
snapshots into the lines that appeared and the ones that name a handle, so a
watcher can act on "a new line says @mars" without a person pinging it.

Imports nothing: pure text rules, testable without pycrdt.
"""
from __future__ import annotations

import re
from collections import Counter


def new_lines(before: str, after: str) -> list[str]:
    """Lines present in `after` that were not in `before`, in document order.

    Counted, not set-differenced: a line that already appeared once and now
    appears twice was written once more, and the second copy is new.
    """
    left = Counter(line for line in before.splitlines() if line.strip())
    out = []
    for line in after.splitlines():
        if not line.strip():
            continue
        if left[line] > 0:
            left[line] -= 1
        else:
            out.append(line)
    return out


def _handle_pattern(handle: str) -> re.Pattern:
    # An @-mention, as a whole token: "@mars" must not fire on "@marshall" nor
    # on a bare "mars" in prose — a summon always writes the @.
    body = handle.lstrip("@")
    # ".", ":" and "-" continue a handle only when a word character follows
    # ("@mars.b", "@mars:x"); "@mars." at a sentence end is still @mars.
    return re.compile(r"(?<!\w)@" + re.escape(body) + r"(?!\w|[.:-]\w)", re.I)


def addressed_to(lines: list[str], handles: list[str]) -> list[str]:
    """The lines that @-mention any of `handles` (given with or without the @)."""
    patterns = [_handle_pattern(h) for h in handles if h.strip("@").strip()]
    return [line for line in lines if any(p.search(line) for p in patterns)]
