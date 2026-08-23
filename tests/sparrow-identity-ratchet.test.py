#!/usr/bin/env python3
"""The identity ratchet (B slice 2): src/ may not grow new identity mints.

Two one-way gates over src/*.py, pinned to today's shipped sites:

R-A  Wall-clock task minting (f"task-{...}" literals). The census showed a
     replayed provider event becomes a NEW task wherever the id comes from
     the clock; new code must use ag2_sparrow.identity.ingress_task_id.
     A count above the pin is a new mint site (red). A count below the pin
     means a site was strangled — tighten the pin in the same change (red
     until you do, so progress is recorded, not lost).

R-B  delivery_id constructor exclusivity: a src/ file that names
     delivery_id must either predate this ratchet (pinned below) or import
     the canonical constructors from ag2_sparrow.identity.

Run: python3 tests/sparrow-identity-ratchet.test.py   (stdlib only)
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# f"task-{...}" literals per file, as shipped at the ratchet's introduction.
TASK_MINT_PIN = {
    "agent-api.py": 4,
    "discord-bridge.py": 3,
    "obsidian-mirror.py": 1,
    "slack-bridge.py": 2,
    "telegram-bridge.py": 2,
}
_TASK_MINT = re.compile(r'f"task-\{')

# Files that referenced delivery_id before the canonical package existed.
DELIVERY_ID_LEGACY_FILES = {
    "slack-bridge.py",
    "slack_proactive_receipts.py",
}
_CANONICAL_IMPORT = re.compile(r"from\s+ag2_sparrow\.identity\s+import|"
                               r"from\s+ag2_sparrow\s+import\s+identity")


class WallClockTaskMintRatchet(unittest.TestCase):
    def test_no_new_mint_sites_and_pins_track_removals(self):
        counts = {}
        for py in sorted(SRC.glob("*.py")):
            n = len(_TASK_MINT.findall(py.read_text(errors="replace")))
            if n:
                counts[py.name] = n
        for name, n in sorted(counts.items()):
            pinned = TASK_MINT_PIN.get(name, 0)
            self.assertLessEqual(
                n, pinned,
                f"src/{name} has {n} f\"task-{{...}}\" mint site(s), pin is "
                f"{pinned}. New task identities must come from "
                f"ag2_sparrow.identity.ingress_task_id (injective, "
                f"replay-stable), not the wall clock.")
            self.assertGreaterEqual(
                n, pinned,
                f"src/{name} dropped below its pin ({n} < {pinned}) — a mint "
                f"site was strangled. Lower TASK_MINT_PIN in this test in the "
                f"same change so the ratchet records the progress.")
        for name, pinned in sorted(TASK_MINT_PIN.items()):
            self.assertIn(name, counts,
                          f"pinned file src/{name} has no mint sites left — "
                          f"remove its TASK_MINT_PIN entry.")


class DeliveryIdConstructorExclusivity(unittest.TestCase):
    def test_new_delivery_id_users_import_the_canonical_package(self):
        for py in sorted(SRC.glob("*.py")):
            text = py.read_text(errors="replace")
            if "delivery_id" not in text:
                continue
            if py.name in DELIVERY_ID_LEGACY_FILES:
                continue
            self.assertRegex(
                text, _CANONICAL_IMPORT,
                f"src/{py.name} names delivery_id but does not import "
                f"ag2_sparrow.identity — the canonical constructors are the "
                f"only legal source of a delivery identity (freeze doc R1/R3).")


if __name__ == "__main__":
    unittest.main()
