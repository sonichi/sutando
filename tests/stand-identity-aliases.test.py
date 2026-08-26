#!/usr/bin/env python3
"""A variant spelling of my own stand must not read as a peer.

The live failure: ownership compared a `Stand:` trailer against one remembered
name, so `Sutando-Pro` and `Sutando-Pro (principal)` — both written by me —
were filed as another agent's and two of my own PRs went unworked.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stand_identity import is_my_stand, my_stand_aliases  # noqa: E402

HOST = "Chis-MacBook-Pro"


def _ws(name="Echo Act IV Pro", machine="macbook-pro") -> Path:
    w = Path(tempfile.mkdtemp(prefix="stand-"))
    d = w / "hosts" / HOST
    d.mkdir(parents=True)
    (d / "stand-identity.json").write_text(
        json.dumps({"name": name, "machine": machine}))
    return w


class StandIdentityTest(unittest.TestCase):
    def test_the_two_spellings_that_were_disowned_are_mine(self) -> None:
        w = _ws()
        for t in ("Sutando-Pro", "Sutando-Pro (principal)", "Echo Act IV Pro"):
            with self.subTest(trailer=t):
                self.assertTrue(is_my_stand(t, w, HOST), f"{t!r} is mine")

    def test_a_peer_stand_is_not_mine(self) -> None:
        """The check must still be able to say NO, or it certifies nothing."""
        w = _ws()
        for t in ("Echo Act IV Mini", "Sutando-Mini", "Sutando-rui", "PRO-ish"):
            with self.subTest(trailer=t):
                self.assertFalse(is_my_stand(t, w, HOST), f"{t!r} is NOT mine")

    def test_identity_comes_from_disk_not_from_a_constant(self) -> None:
        """Point it at a Mini workspace and the Pro spellings stop matching."""
        w = _ws(name="Echo Act IV Mini", machine="mac-mini")
        self.assertTrue(is_my_stand("Sutando-Mini", w, HOST))
        self.assertFalse(is_my_stand("Sutando-Pro", w, HOST))

    def test_aliases_are_normalised_not_literal(self) -> None:
        w = _ws()
        self.assertIn("sutandopro", my_stand_aliases(w, HOST))
        self.assertTrue(is_my_stand("  sutando pro  ", w, HOST))


if __name__ == "__main__":
    unittest.main(verbosity=2)
