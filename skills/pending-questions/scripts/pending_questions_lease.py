"""Elapsed-time leases that a wall-clock step can neither cut short nor stretch.

Invariant: a lease expires exactly when the time credited to it reaches its window. A pass credits
clamp(now - observed_at, 0, max_step), where `observed_at` is the latest wall time any pass has
seen for this lease and only ever moves forward (observed_at = max(observed_at, now)). Hence:

- a backward step credits 0, and catching back up to the earlier `observed_at` credits 0 too;
- repeated passes at one wall time credit 0;
- a forward jump credits at most max_step on that pass, so no single step expires a lease whose
  window is larger than max_step;
- only deltas between passes count, never how far a stamp is from this host's clock;
- a new lease (a handoff, a re-offer, a fresh claim) starts at elapsed 0, observed_at = now.

A lease is a plain dict {"observed_at", "elapsed"} stored on the record it guards, written in the
same atomic store save under the caller's lock, so a restart resumes from the persisted values.
"""
from __future__ import annotations

from typing import Optional


def new(now: float) -> dict:
    return {"observed_at": float(now), "elapsed": 0.0}


def advance(lease: Optional[dict], now: float, max_step: float) -> dict:
    """The lease after one pass at `now`. A record with no lease starts one now."""
    if not lease or lease.get("observed_at") is None:
        return new(now)
    seen = float(lease["observed_at"])
    step = min(max(float(now) - seen, 0.0), float(max_step))
    return {"observed_at": max(seen, float(now)), "elapsed": float(lease.get("elapsed") or 0.0) + step}


def expired(lease: Optional[dict], window: float) -> bool:
    return float((lease or {}).get("elapsed") or 0.0) >= float(window)
