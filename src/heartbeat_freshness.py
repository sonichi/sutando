#!/usr/bin/env python3
"""One definition of "is this heartbeat fresh?" for the pool modules.

A heartbeat age must be bounded at BOTH ends. A future-dated mtime makes
`now - mtime` negative, and a negative age passes any upper bound, so a
one-sided check reports a dead writer as alive for as long as the skew lasts.
Stdlib only, no imports: every reader of a `.alive`-class file can take this.
"""
from __future__ import annotations

HEARTBEAT_FUTURE_TOLERANCE_S = 5.0


def age_is_fresh(age: float, max_age: float,
                 tolerance: float = HEARTBEAT_FUTURE_TOLERANCE_S) -> bool:
    """True only inside [-tolerance, max_age). The lower bound is the half
    a one-sided `age < max_age` omits, which is what lets skew read as alive."""
    return -tolerance <= age < max_age
