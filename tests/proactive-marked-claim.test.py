#!/usr/bin/env python3
"""A [channel:]-addressed proactive must not be stranded by the round gate.

should_claim_proactive answers "whose turn is it" per POLLING ROUND, before any
file is opened. Destination is a property of the FILE. A body explicitly routed
to one transport was therefore stranded whenever the owner was last active
elsewhere -- not delivered, not quarantined, just left in results/.

Run: python3 tests/proactive-marked-claim.test.py"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import proactive_routing  # noqa: E402
from proactive_routing import (  # noqa: E402
    claims_marked_proactive, should_claim_proactive)

FAILS = []


def check(name, cond):
    print(f"  {'ok' if cond else 'FAIL'}: {name}")
    if not cond:
        FAILS.append(name)


DISCORD = "1494826369962479688"
AG2 = "!ZrUcWFEBQsISfUNOGF:ag2.space"


def main() -> int:
    check("discord id claimed by discord",
          claims_marked_proactive(f"[channel: {DISCORD}]\nx", "discord") is True)
    check("discord id refused by telegram",
          claims_marked_proactive(f"[channel: {DISCORD}]\nx", "telegram") is False)
    check("ag2space id claimed by ag2space",
          claims_marked_proactive(f"[channel: {AG2}]\nx", "ag2space") is True)
    check("ag2space id refused by discord",
          claims_marked_proactive(f"[channel: {AG2}]\nx", "discord") is False)

    # None => "cannot attribute", so the caller keeps the last-active heuristic.
    check("unmarked body is not attributable",
          claims_marked_proactive("plain body", "discord") is None)
    check("ambiguous short numeric is not attributable",
          claims_marked_proactive("[channel: 12345]\nx", "discord") is None)

    # The incident shape: owner last active on ag2space, body addressed to
    # Discord. The round gate says no; the marker must still carry it.
    with tempfile.TemporaryDirectory() as td:
        st = Path(td) / "last-owner-activity.json"
        st.write_text(json.dumps({"channel": "ag2space"}))
        check("round gate refuses discord while owner is on ag2space",
              should_claim_proactive(st, "discord") is False)
        check("but the discord-addressed body is still claimable",
              claims_marked_proactive(f"[channel: {DISCORD}]\nx", "discord") is True)

    src = (REPO / "src" / "discord-bridge.py").read_text()
    check("discord-bridge consults the marker policy",
          "claims_marked_proactive" in src)
    check("discord-bridge no longer skips the round unconditionally",
          "if not _round_claim:" in src)
    # The bridge looks the policy up with getattr; pin that the real module
    # exports it, so that tolerance cannot become a silent revert.
    check("the real module exports the policy the getattr looks up",
          callable(getattr(proactive_routing, "claims_marked_proactive", None)))

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: a [channel:]-addressed proactive survives the round gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
