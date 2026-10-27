#!/usr/bin/env python3
"""The shared ack claim (src/ack_claim.py): first writer wins, only the holder changes it.

  a) N processes race claim() on one key through the production writer: exactly one wins
  b) a second claim of a held key loses, whoever asks
  c) only the holder may update or release; another holder is refused and nothing changes
  d) an unreadable or foreign-shaped record reads as held ("unknown"), never as free
  e) a malformed key is refused, never turned into a path
  f) release/update bound to a claim generation never touch a successor created after a release
  CONTROL: a check-then-replace writer lets both racers win, so (a)/(b) can fail

Run: python3 tests/ack-claim.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import ack_claim  # noqa: E402
import local_record  # noqa: E402

FAILS: list = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


RACER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import ack_claim
from pathlib import Path
start = float(sys.argv[4])
while time.time() < start:
    pass
print("WON" if ack_claim.claim(Path(sys.argv[2]), "ack:task-1761300000001:initial", sys.argv[3]) else "LOST")
"""


def replace_writer(ws: Path, key: str, holder: str) -> bool:
    """CONTROL: exists() then write-whole; both racers can pass the check before either writes."""
    p = ack_claim._path(ws, key)
    if p.exists():
        return False
    return True


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="ack-claim-"))
    key = "ack:task-1761300000001:initial"
    import time
    start = time.time() + 1.0
    procs = [subprocess.Popen([sys.executable, "-c", RACER, str(REPO / "src"), str(ws), f"racer-{i}", str(start)],
                              stdout=subprocess.PIPE, text=True) for i in range(8)]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    check(outs.count("WON") == 1 and outs.count("LOST") == 7, f"a) one of 8 racing processes wins ({outs})")
    rec = ack_claim.read(ws, key) or {}
    check(rec.get("state") == "claimed" and str(rec.get("holder", "")).startswith("racer-"),
          f"a) the record names the winner ({rec.get('holder')}, {rec.get('state')})")

    check(not ack_claim.claim(ws, key, rec.get("holder")), "b) the winner itself cannot claim again")
    check(not ack_claim.claim(ws, key, "task-progress"), "b) another sender loses a held key")

    winner = rec["holder"]
    check(not ack_claim.update(ws, key, "someone-else", "confirmed"), "c) a non-holder cannot update")
    check(ack_claim.state(ws, key) == "claimed", "c) and the state is unchanged")
    check(not ack_claim.release(ws, key, "someone-else") and ack_claim.state(ws, key) == "claimed",
          "c) a non-holder cannot release")
    check(ack_claim.update(ws, key, winner, "confirmed", provider_ref="$e1")
          and ack_claim.read(ws, key).get("provider_ref") == "$e1", "c) the holder settles it with evidence")
    check(ack_claim.release(ws, key, winner) and ack_claim.state(ws, key) == "none", "c) the holder may release")

    k2 = "ack:task-1761300000002:initial"
    ack_claim._path(ws, k2).write_text("{not json", encoding="utf-8")
    check(ack_claim.state(ws, k2) == "unknown" and not ack_claim.claim(ws, k2, "x"),
          "d) an unreadable record is held, not free")
    k3 = "ack:task-1761300000003:initial"
    ack_claim._path(ws, k3).write_text(json.dumps({"key": "ack:other:initial", "state": "claimed"}), encoding="utf-8")
    check(ack_claim.state(ws, k3) == "unknown", "d) a record naming another key reads unknown")

    for bad in ("ack:../x:initial", "ack:task-1:", "ack task-1 initial", "", "ack:a/b:initial"):
        try:
            ack_claim.claim(ws, bad, "x")
            refused = False
        except ValueError:
            refused = True
        check(refused, f"e) malformed key {bad!r} refused")

    try:
        ack_claim.update(ws, k2, "x", "delivered")
        refused = False
    except ValueError:
        refused = True
    check(refused, "e) an unknown state is refused")
    ack_claim.claim(ws, "ack:task-1761300000005:initial", "mine")
    held = [r["key"] for r in ack_claim.held_by(ws, "mine")]
    check(held == ["ack:task-1761300000005:initial"], f"d) held_by skips unreadable records ({held})")

    k6 = "ack:task-1761300000006:initial"
    g1 = ack_claim.claim(ws, k6, "runner")
    check(isinstance(g1, str) and ack_claim.release(ws, k6, "runner", gen=g1), "f) a claim carries its generation")
    g2 = ack_claim.claim(ws, k6, "runner")
    check(isinstance(g2, str) and g2 != g1, "f) the republished claim is a new generation")
    check(not ack_claim.release(ws, k6, "runner", gen=g1) and ack_claim.state(ws, k6) == "claimed",
          "f) a stale release of the old generation leaves the successor")
    check(not ack_claim.update(ws, k6, "runner", "confirmed", gen=g1) and ack_claim.state(ws, k6) == "claimed",
          "f) a stale update of the old generation leaves the successor")
    check(ack_claim.update(ws, k6, "runner", "confirmed", gen=g2) and ack_claim.state(ws, k6) == "confirmed",
          "f) the current generation updates")
    ack_claim.release(ws, k6, "runner")
    g3 = ack_claim.claim(ws, k6, "runner")
    check(ack_claim.release(ws, k6, "runner") and ack_claim.state(ws, k6) == "none" and g3,
          "CONTROL: without a generation, the holder name alone deletes the successor")

    # CONTROL: interleave two check-then-replace writers by hand: both pass the check.
    k4 = "ack:task-1761300000004:initial"
    a = replace_writer(ws, k4, "a")
    b = replace_writer(ws, k4, "b")
    local_record.write_whole(ack_claim._path(ws, k4), {"id": "x", "key": k4, "holder": "a", "state": "claimed"})
    check(a and b, "CONTROL: a check-then-replace writer lets both interleaved racers speak")
    print(f"\nack-claim: {'FAIL' if FAILS else 'PASS'} ({len(FAILS)} failure(s))")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
