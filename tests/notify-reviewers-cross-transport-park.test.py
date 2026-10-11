#!/usr/bin/env python3
"""A park that only one transport honours is not a park.

`claim_park` used to run inside the Discord branch only, so naming the same
person's MATRIX alias walked straight past an active `unknown` park and could
duplicate a send that had already landed. The stale-repeat guard does not cover
it: asks younger than 30 minutes intentionally pass.

Run: python3 tests/notify-reviewers-cross-transport-park.test.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"

# One person, two spellings, two transports.
ROSTER = {
    "d": {"discord_id": "111", "home_channel": "222", "allowlisted": True},
    "m": {"stand": "@d-stand:x", "room": "!triage:x", "allowlisted": True,
          "same_actor_as": "d"},
}
MSG = ["--message", "re-review https://github.com/o/r/pull/7"]
# from the module: {2, 10} are the ONLY codes that prove nothing posted.
PROVEN_NOT_DELIVERED = 2


def _load():
    spec = importlib.util.spec_from_file_location("nr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class CrossTransportPark(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = Path(self.tmp.name) / "ledger.jsonl"

    def _invoke(self, who, effect=None, rc=0):
        """One full main() run against a stubbed transport. Returns (rc, sends)."""
        sends = {"n": 0}
        members = json.dumps({"ok": True, "members": [{"user_id": "@d-stand:x"}]})
        sent = json.dumps({"ok": True, "event_id": "$e"})

        def fake_run(cmd, *a, **k):
            flat = " ".join(str(x) for x in cmd)
            if "members" in flat:
                return type("R", (), {"stdout": members, "stderr": "", "returncode": 0})()
            if "collaborators" in flat:
                return type("R", (), {"stdout": "write", "stderr": "", "returncode": 0})()
            if "users/" in flat:
                return type("R", (), {"stdout": "someone", "stderr": "", "returncode": 0})()
            sends["n"] += 1
            if effect is not None:
                raise effect
            return type("R", (), {"stdout": sent, "stderr": "", "returncode": rc})()

        argv = ["notify_reviewers.py", "--reviewers", who, "--send",
                "--allow-single", "regression fixture"] + MSG
        with patch.object(self.mod, "load_roster", return_value=ROSTER), \
             patch.object(self.mod, "ledger_path", return_value=self.led), \
             patch.object(self.mod, "discord_reachable", return_value=(True, "verified")), \
             patch.object(self.mod, "stand_present_in_room", return_value=(True, "verified")), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch("sys.argv", argv):
            return self.mod.main(), sends["n"]

    def _park_via_discord_timeout(self):
        rc, sends = self._invoke("d", effect=subprocess.TimeoutExpired("x", 60))
        self.assertEqual(sends, 1, "the fixture never attempted the first send")
        return rc

    def test_a_MATRIX_alias_does_not_walk_past_a_park_claimed_on_DISCORD(self):
        """The blocker. Different transport, same person, park still holds."""
        self._park_via_discord_timeout()
        rc, sends = self._invoke("m")
        self.assertEqual(sends, 0,
                         "the Matrix alias sent anyway — a landed post may now be duplicated")
        self.assertEqual(rc, 4, "an honoured park exits 4 (unsafe to repeat)")

    def test_CONTROL_the_same_transport_is_parked_too(self):
        """If this fails the harness never observed a real park, and the
        blocker case above would pass for the wrong reason."""
        self._park_via_discord_timeout()
        rc, sends = self._invoke("d")
        self.assertEqual(sends, 0, "the same-transport repeat was not parked")
        self.assertEqual(rc, 4)

    def test_CONTROL_a_proven_nondelivery_still_allows_the_alias_to_retry(self):
        """The park must not swallow legitimate retries: a proven non-send
        releases, and the other transport may then carry the ask."""
        rc, sends = self._invoke("d", rc=PROVEN_NOT_DELIVERED)
        self.assertEqual(sends, 1)
        rc2, sends2 = self._invoke("m")
        self.assertEqual(sends2, 1,
                         "a proven non-delivery was treated as unsafe to repeat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
