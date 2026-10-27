"""Elapsed-time leases that no wall-clock step can expire early or freeze.

Invariant: a lease expires exactly when the time credited to it reaches its window. A pass credits
clamp(now - observed_at, 0, max_step) and then sets observed_at = now (the wall time this pass saw,
forward or back). With max_step < window:

- a backward step credits 0, so a rollback never expires a lease early;
- a forward jump, however large, credits at most max_step, and once the clock is corrected normal
  deltas resume: a spike costs at most max_step of extra credit, never a freeze;
- repeated passes at one wall time credit 0;
- no single pass expires a fresh lease: it takes at least window/max_step passes;
- a new lease (a handoff, a re-offer, a fresh claim) starts at elapsed 0, observed_at = now.

The trade-off, chosen on purpose: after a rollback, catching back up is credited again, so a
rollback can over-credit by at most max_step per pass that sees it. Refusing all double credit
(keeping observed_at at its high water) would instead freeze the lease for the whole span of a
forward spike that is later corrected, which is unbounded.

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
    step = min(max(float(now) - float(lease["observed_at"]), 0.0), float(max_step))
    return {"observed_at": float(now), "elapsed": float(lease.get("elapsed") or 0.0) + step}


def expired(lease: Optional[dict], window: float) -> bool:
    return float((lease or {}).get("elapsed") or 0.0) >= float(window)
