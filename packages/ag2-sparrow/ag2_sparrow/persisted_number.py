"""One reader for a number parsed from a persisted record (JSON on disk).

`bounded_number` accepts only an int or a float, never a bool, inside the caller's range, and a
float only when finite. The range is checked before any conversion, so an int too large for a
float (10**400) is refused rather than raised. Stdlib only."""
from __future__ import annotations

import math


def bounded_number(value, lo: float, hi: float) -> "float | None":
    """`value` as a float when it is an int or a finite float within [lo, hi]; None otherwise."""
    if type(value) is int:
        return float(value) if lo <= value <= hi else None
    if type(value) is float and math.isfinite(value) and lo <= value <= hi:
        return value
    return None
