#!/usr/bin/env python3
"""ProactiveClaimFence — the 5b claim-substrate swap, driven on a REAL
ClaimBackend (Design A; the suite is contract-shaped, so Design C slots in
when it ships):

  FILE    every transition pairs the documented file move (peer-glob
          visibility is unchanged from the private .sending machinery).
  DURABLE transient attempt counts survive a fence/process restart — the
          in-memory dict the fence replaces lost them (real defect).
  PARK    partial delivery parks (never re-sends confirmed chunks); the
          backend record parks with it.
  RECOVER a crash between file move and backend transition reconciles.

Run: python3 tests/proactive-claim-fence.test.py"""
# ruff: noqa: E402 — imports follow the sys.path inserts below
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import DesignAClaimBackend
from proactive_claim_fence import ProactiveClaimFence
from send_failure_policy import UnconfirmedDelivery, MAX_TRANSIENT_ATTEMPTS

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def _fence(root: Path) -> ProactiveClaimFence:
    return ProactiveClaimFence(
        DesignAClaimBackend(root / ".outbox-test"), root, worker="t")


class _Boom(Exception):
    status = 413  # permanent: a payload too large never becomes a 200


def main() -> int:
    # FILE + confirm: claim moves .txt aside; confirm consumes it.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-a.txt"
        body.write_text("hello")
        fence = _fence(root)
        claim = fence.claim(body)
        check("claim returns the .sending path",
              claim == root / "proactive-a.sending")
        check("claim moves the body out of the .txt glob",
              not body.exists() and claim.exists())
        check("second claim of a gone file is None", fence.claim(body) is None)
        fence.confirm(claim)
        check("confirm consumes the claim file", not claim.exists())

    # release: unready body returns to the polling stream.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-b.txt"
        body.write_text("x")
        fence = _fence(root)
        claim = fence.claim(body)
        check("release restores the .txt", fence.release(claim) and body.exists())

    # DURABLE: transient attempts survive a new fence over the same root —
    # the cap is enforceable across restarts, unlike the in-memory dict.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-c.txt"
        body.write_text("x")
        transient = UnconfirmedDelivery("no event id")
        outcomes = []
        for _ in range(MAX_TRANSIENT_ATTEMPTS + 1):
            fence = _fence(root)          # fresh instance = simulated restart
            claim = fence.claim(body)
            outcomes.append(fence.fail(claim, transient, progressed=False))
        check("transient failures retry up to the cap",
              outcomes[:MAX_TRANSIENT_ATTEMPTS] == ["retried"] * MAX_TRANSIENT_ATTEMPTS,
              str(outcomes))
        check("attempt count survives restarts: capped run parks",
              outcomes[-1] == "parked", str(outcomes))
        check("parked body is byte-preserved in undelivered/",
              (root / "undelivered" / "proactive-c.txt").read_text() == "x")

    # PARK: progressed=True parks even a transient failure (chunk 1 landed).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-d.txt"
        body.write_text("y")
        fence = _fence(root)
        claim = fence.claim(body)
        out = fence.fail(claim, UnconfirmedDelivery("after 1 chunk"),
                         progressed=True)
        check("partial delivery parks, never retries", out == "parked")

    # PARK: permanent failure parks on the first attempt.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-e.txt"
        body.write_text("z")
        fence = _fence(root)
        claim = fence.claim(body)
        check("permanent failure parks first time",
              fence.fail(claim, _Boom(), progressed=False) == "parked")

    # RECOVER: crash after the file move, before/instead of a clean cycle —
    # a stranded .sending returns to the polling stream on the next start.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-f.txt"
        body.write_text("w")
        fence = _fence(root)
        fence.claim(body)  # token dropped on the floor = crashed process
        fence2 = _fence(root)
        recovered = fence2.recover()
        check("recover restores the stranded .sending", recovered == 1
              and body.exists(), f"recovered={recovered}")
        claim = fence2.claim(body)
        check("recovered body is claimable again", claim is not None)
        fence2.confirm(claim)

    # re-written body is a fresh cycle: attempts do not carry across mtimes.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = root / "proactive-g.txt"
        body.write_text("v1")
        fence = _fence(root)
        claim = fence.claim(body)
        fence.fail(claim, UnconfirmedDelivery("x"), progressed=False)  # retried
        import os
        os.utime(body, ns=(1, 1))  # force a distinct mtime identity
        fence2 = _fence(root)
        claim = fence2.claim(body)
        check("re-written body starts with zero attempts",
              fence2.attempts(claim) == 0)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: proactive claim fence — file semantics unchanged, "
          "attempts durable, partial delivery parks, recovery reconciles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
