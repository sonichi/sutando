"""check_onboarding_status: the core-side reader of the desktop checklist's
agent surface (onboarding v2, ag2space-cinny-desktop#165 S4).

Covers: absent file → None; todo rows → warn naming them; all-satisfied → ok;
unreadable → warn (never raises).
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)


class OnboardingStatusCheckTest(unittest.TestCase):
    def _with_workspace(self, payload):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name)
        if payload is not None:
            (ws / "state").mkdir()
            f = ws / "state" / "onboarding-status.json"
            if isinstance(payload, str):
                f.write_text(payload)
            else:
                f.write_text(json.dumps(payload))
        orig = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = ws
        self.addCleanup(lambda: setattr(hc, "WORKSPACE_DIR", orig))
        return ws

    def test_none_when_file_absent(self):
        self._with_workspace(None)
        self.assertIsNone(hc.check_onboarding_status())

    def test_warn_names_todo_rows(self):
        self._with_workspace(
            {
                "updated_at": 0,
                "rows": {
                    "core": {"state": "done"},
                    "gateway": {"state": "todo"},
                    "voice_creds": {"state": "optional"},
                },
            }
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("gateway", out["detail"])
        self.assertNotIn("core,", out["detail"])

    def test_ok_when_no_todo(self):
        self._with_workspace(
            {"updated_at": 0, "rows": {"core": {"state": "done"}, "accessibility": {"state": "optional"}}}
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "ok")

    def test_warn_on_list_payload(self):
        # Codex P1: a top-level list must degrade to 'unreadable', not raise.
        self._with_workspace("[]")
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])

    def test_warn_on_list_rows(self):
        # Codex P1: rows as a list (frontend bug) must also degrade cleanly.
        self._with_workspace({"updated_at": 0, "rows": []})
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])

    def test_ok_with_null_updated_at(self):
        self._with_workspace({"updated_at": None, "rows": {"core": {"state": "done"}}})
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "ok")

    def test_warn_on_unreadable(self):
        self._with_workspace("{not json")
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("unreadable", out["detail"])


    def test_a_todo_row_carries_its_own_detail(self):
        """`gateway` alone cannot distinguish "not running" from a reconnect —
        the writer populates `detail` to say which, and it was being dropped."""
        self._with_workspace(
            {
                "updated_at": 0,
                "rows": {
                    "gateway": {"state": "todo",
                                "detail": "gateway process up, relay not connected"},
                    "core": {"state": "todo"},
                },
            }
        )
        out = hc.check_onboarding_status()
        self.assertEqual(out["status"], "warn")
        self.assertIn("gateway (gateway process up, relay not connected)", out["detail"])
        # A row with no detail still renders as the bare name.
        self.assertIn("core", out["detail"])
        self.assertNotIn("core (", out["detail"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
