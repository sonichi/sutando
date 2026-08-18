#!/usr/bin/env python3
"""Explicit proactive destinations (owner design 2026-08-18): a destined
filename is claimed ONLY by its target bridge; undestined names keep the
last-activity routing. The grammar must survive every existing discovery
glob and the claim-rename cycle, or a destined file silently re-enters
the race this feature exists to end.

Run: python3 tests/proactive-destination.test.py"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import fnmatch
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import (PROACTIVE_DESTINATIONS, proactive_destination,
                               proactive_filename,
                               should_claim_proactive_file)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def _state(tmp, channel):
    p = Path(tmp) / "last-owner-activity.json"
    p.write_text(json.dumps({"channel": channel, "ts": 1}))
    return p


def main() -> int:
    # Grammar round-trip + constructor is the only legal spelling.
    n = proactive_filename(1234, "discord")
    check("round-trip", proactive_destination(n) == "discord", n)
    check("undestined round-trips to None",
          proactive_destination(proactive_filename(1234)) is None)
    try:
        proactive_filename(1, "smoke-signal")
        check("unknown destination refused at construction", False)
    except ValueError:
        check("unknown destination refused at construction", True)
    check("every bridge channel is a legal destination",
          all(proactive_destination(proactive_filename(1, c)) == c
              for c in PROACTIVE_DESTINATIONS))

    # Discovery compatibility: every existing poller's filter still sees it.
    check("gateway glob still discovers destined files",
          fnmatch.fnmatch(n, "proactive-*.txt"))
    check("telegram prefix+suffix filter still discovers destined files",
          n.startswith("proactive-") and Path(n).suffix == ".txt")

    # Claim-rename cycle preserves the tag (with_suffix touches only .txt).
    claimed = Path(n).with_suffix(".sending.4242")
    check("claim rename keeps the destination tag",
          ".to-discord." in claimed.name)
    # Model the PRODUCTION recovery expression (both recovery sites use
    # name.split(".sending")[0] + ".txt"), not with_suffix.
    recovered = claimed.name.split(".sending")[0] + ".txt"
    check("recovery rename restores a claimable destined name",
          recovered == n and proactive_destination(recovered) == "discord")

    # Per-file decision: destination outranks activity routing BOTH ways.
    with tempfile.TemporaryDirectory() as td:
        st = _state(td, "ag2space")
        check("destined file claimed by target even when activity is elsewhere",
              should_claim_proactive_file(n, st, "discord") is True)
        check("destined file refused to the activity-preferred bridge",
              should_claim_proactive_file(n, st, "ag2space") is False)
        legacy = proactive_filename(1234)
        check("undestined file follows activity routing (preferred claims)",
              should_claim_proactive_file(legacy, st, "ag2space") is True)
        check("undestined file follows activity routing (other declines)",
              should_claim_proactive_file(legacy, st, "discord") is False)
        # A tag this install can't parse to a known channel still blocks all:
        # strand visibly, never leak into the race.
        alien = "proactive-1.to-futurechan.txt"
        check("unrecognized tag reads as a destination (blocks every bridge)",
              all(should_claim_proactive_file(alien, st, c) is False
                  for c in ("discord", "telegram", "ag2space")))

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: proactive destinations — grammar, glob/claim survival, "
          "destination-outranks-activity, visible stranding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
