"""One reader for a number from untrusted JSON (a ledger, a record, a payload).

`finite(v)` is the number as a finite float, or None for anything else: a bool, a string, NaN, an
infinity, or an int too large for a float (which `math.isfinite` and arithmetic both raise on).
Vendored into ag2-sparrow by tools/sync_from_src.py.
"""
from __future__ import annotations

import math


def finite(v) -> "float | None":
    if type(v) not in (int, float):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None
