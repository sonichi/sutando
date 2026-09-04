#!/usr/bin/env python3
"""A Matrix send that MAY have landed must park, not release.

room_ops' mention used to answer a transport timeout with a bare ok:false, and the
notifier read every ok:false as proven non-delivery — so a second invocation sent
again. A 200 with no event id was read as confirmed the same way. Both are the
receipt contract's ambiguous states; they must record `unknown` and hold the park.

Run: python3 tests/notify-reviewers-matrix-tristate.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"
MENTION = REPO / "skills" / "agent-room-ops" / "mention.py"
ROSTER = {"m": {"stand": "@m-stand:x", "room": "!triage:x", "allowlisted": True}}
MSG = ["--message", "re-review https://github.com/o/r/pull/7"]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


class NotifierMapsTheTriState(unittest.TestCase):
    def setUp(self):
        self.mod = _load(SCRIPT, "nr")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = Path(self.tmp.name) / "ledger.jsonl"

    def _invoke(self, child_payload):
        sends = {"n": 0}
        members = json.dumps({"ok": True, "members": [{"user_id": "@m-stand:x"}]})

        def fake_run(cmd, *a, **k):
            flat = " ".join(str(x) for x in cmd)
            if "members" in flat:
                return type("R", (), {"stdout": members, "stderr": "", "returncode": 0})()
            if "collaborators" in flat:
                return type("R", (), {"stdout": "write", "stderr": "", "returncode": 0})()
            if "users/" in flat:
                return type("R", (), {"stdout": "someone", "stderr": "", "returncode": 0})()
            if "mention" not in flat:
                return type("R", (), {"stdout": "{}", "stderr": "", "returncode": 0})()
            sends["n"] += 1
            return type("R", (), {"stdout": json.dumps(child_payload), "stderr": "",
                                  "returncode": 0})()

        argv = ["notify_reviewers.py", "--reviewers", "m", "--send",
                "--allow-single", "regression fixture"] + MSG
        with patch.object(self.mod, "load_roster", return_value=ROSTER), \
             patch.object(self.mod, "ledger_path", return_value=self.led), \
             patch.object(self.mod, "stand_present_in_room", return_value=(True, "verified")), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch("sys.argv", argv):
            return self.mod.main(), sends["n"]

    def _outcomes(self):
        return [json.loads(l)["outcome"] for l in self.led.read_text().splitlines() if l.strip()]

    def test_an_inner_timeout_parks_and_the_second_invocation_does_not_resend(self):
        payload = {"ok": False, "reason": "network error: timed out", "state": "unknown"}
        rc1, s1 = self._invoke(payload)
        rc2, s2 = self._invoke(payload)
        self.assertEqual((s1, s2), (1, 1), "the ambiguous send must not be repeated")
        self.assertIn("unknown", self._outcomes())
        self.assertNotIn("failed", self._outcomes())

    def test_a_200_without_an_event_id_is_unknown_not_confirmed(self):
        payload = {"ok": True, "event_id": None, "state": "unconfirmed"}
        rc1, s1 = self._invoke(payload)
        rc2, s2 = self._invoke(payload)
        self.assertEqual((s1, s2), (1, 1))
        self.assertNotIn("confirmed", self._outcomes())
        self.assertIn("unknown", self._outcomes())

    def test_the_control_a_definite_refusal_still_licenses_a_retry(self):
        payload = {"ok": False, "reason": "HTTP 403 not a joined member", "state": "failed"}
        rc1, s1 = self._invoke(payload)
        rc2, s2 = self._invoke(payload)
        self.assertEqual((s1, s2), (1, 2), "a proven non-delivery releases the park")
        self.assertEqual(self._outcomes().count("failed"), 2)

    def test_the_control_an_event_id_is_confirmed(self):
        rc, s = self._invoke({"ok": True, "event_id": "$e", "state": "confirmed"})
        self.assertEqual(rc, 0)
        self.assertIn("confirmed", self._outcomes())


class MentionCarriesTheReceiptState(unittest.TestCase):
    def setUp(self):
        self.m = _load(MENTION, "mention_t")

    def _send(self, effect):
        with patch.object(self.m, "resolve_user", return_value={"ok": True, "mxid": "@p:x"}), \
             patch.object(self.m, "gate_allows", return_value=True), \
             patch.object(self.m, "gateway", return_value=("http://x", {})), \
             patch.object(self.m, "http_json", side_effect=effect):
            return self.m.mention("@p:x", "hi", "!r:x", "@me:x", gate={})

    def test_a_transport_timeout_is_unknown_not_failed(self):
        res = self._send(TimeoutError("timed out"))
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("state"), "unknown")

    def test_a_200_with_no_event_id_is_unconfirmed(self):
        res = self._send(lambda *a, **k: (200, {}))
        self.assertEqual(res.get("state"), "unconfirmed")
        self.assertIsNone(res.get("event_id"))

    def test_an_event_id_is_confirmed(self):
        res = self._send(lambda *a, **k: (200, {"event_id": "$e"}))
        self.assertEqual((res["ok"], res.get("state"), res["event_id"]), (True, "confirmed", "$e"))

    def test_the_control_an_http_refusal_is_failed(self):
        res = self._send(self.m.HTTPError("u", 403, "forbidden", {}, None))
        self.assertEqual((res["ok"], res.get("state")), (False, "failed"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
