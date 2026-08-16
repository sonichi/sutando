#!/usr/bin/env python3
"""The proactive drain must BOUND its retries instead of re-sending forever.

2026-08-16: one morning-briefing file went out 5 times and a drill file 12,
because every send-failure branch in _post_proactive renamed the claim back to
`.txt` with no ceiling. src/send_failure_policy.py already owned that ceiling
(MAX_TRANSIENT_ATTEMPTS, park-by-default) and ONLY discord-bridge.py used it.

Exercises the production helper `_resolve_send_failure`, not a re-implementation.

Run: python3 tests/proactive-retry-bound.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load_bridge(tmp: str):
    os.environ.update({
        "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": tmp,
        "REMOTE_TASK_URL": "http://127.0.0.1:1", "REMOTE_TASK_TOKEN": "t",
        "REMOTE_TASK_PROVIDER": "remote-gateway",
    })
    # Import as a PACKAGE member: the module uses relative imports (`._dirs`),
    # so loading the file standalone raises "no known parent package".
    sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
    import importlib
    return importlib.import_module("ag2_sparrow.remote_gateway_bridge")


def main() -> int:
    from send_failure_policy import MAX_TRANSIENT_ATTEMPTS, UnconfirmedDelivery, is_transient

    check(is_transient(UnconfirmedDelivery("no event_id")),
          "an accepted-but-unconfirmed send is transient — a momentary withhold recovers")

    with tempfile.TemporaryDirectory() as tmp:
        rgb = load_bridge(tmp)
        results = Path(tmp) / "results"
        results.mkdir(parents=True, exist_ok=True)
        rgb._PROACTIVE_ATTEMPTS.clear()

        original = results / "proactive-1.txt"
        exc = UnconfirmedDelivery("no event_id in response")
        outcomes = []
        for _ in range(MAX_TRANSIENT_ATTEMPTS + 1):
            original.write_text("hi")
            claim = original.with_suffix(".sending.1")   # the drain's pid-scoped form
            original.rename(claim)
            outcomes.append(rgb._resolve_send_failure(claim, original, exc))

        retried = sum(1 for o in outcomes if o.startswith("will retry"))
        check(retried == MAX_TRANSIENT_ATTEMPTS,
              f"retries stop at {MAX_TRANSIENT_ATTEMPTS} (got {retried}) — "
              "without the bound every attempt returns 'will retry' forever")
        check(outcomes[-1].startswith("PARKED"),
              f"the attempt past the ceiling PARKS (got {outcomes[-1]!r})")
        check(not original.exists(),
              "the parked body leaves the polled set, so it cannot be re-sent")
        parked = rgb.UNDELIVERABLE_RESULTS_DIR / original.name
        check(parked.exists() and parked.read_text() == "hi",
              "and is preserved intact for inspection, not deleted")

        # A permanent failure must not consume the budget at all.
        rgb._PROACTIVE_ATTEMPTS.clear()

        class Permanent(Exception):
            status = 404

        other = results / "proactive-2.txt"
        other.write_text("hi")
        claim = other.with_suffix(".sending.1")
        other.rename(claim)
        check(rgb._resolve_send_failure(claim, other, Permanent()).startswith("PARKED"),
              "a permanent failure (404) parks on the FIRST attempt, never retries")

    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — proactive retries are bounded by the shared policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
