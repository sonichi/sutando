#!/usr/bin/env python3
"""Regression: nothing verified that the Claude Code hooks we install stay installed.

2026-08-03, measured on a live host: ZERO of the four hooks `install-claude-hooks.sh`
owns were present in its own target file, including BOTH PreCompact entries. So
session-state.md was never regenerated on compaction and the transcript archiver had
never run — for days, silently, because no probe looks at hook registration. A peer
host showed the same shape.

Run: python3 tests/health-check-claude-hook-registration.test.py
"""
from __future__ import annotations
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_hooks_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


INSTALLER = '''#!/usr/bin/env bash
SETTINGS="$REPO_DIR/.claude/settings.json"
HOOKS=(
  "PreCompact|sutando-conversations/|cp x y"
  "PreCompact|src/session-handoff.sh|bash $REPO_DIR/src/session-handoff.sh"
  "SessionEnd|src/session-handoff.sh|bash $REPO_DIR/src/session-handoff.sh"
  "Stop|src/check-pending-tasks.sh|bash $REPO_DIR/src/check-pending-tasks.sh"
)
'''


class TestHookRegistration(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".claude").mkdir(parents=True)
        (self.repo / "src" / "install-claude-hooks.sh").write_text(INSTALLER)

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, hooks: dict):
        (self.repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))

    def _entry(self, cmd):
        return [{"hooks": [{"type": "command", "command": cmd}]}]

    def _all_registered(self, repo_path=None):
        r = str(repo_path or self.repo)
        return {
            "PreCompact": [{"hooks": [{"command": "cp $TRANSCRIPT_PATH ~/Desktop/sutando-conversations/x.jsonl"},
                                      {"command": f"bash {r}/src/session-handoff.sh"}]}],
            "SessionEnd": self._entry(f"bash {r}/src/session-handoff.sh"),
            "Stop": self._entry(f"bash {r}/src/check-pending-tasks.sh"),
        }

    def test_all_registered_is_ok(self):
        self._settings(self._all_registered())
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "ok", out["detail"])
        self.assertIn("4", out["detail"])

    def test_the_REAL_case_no_hooks_at_all_warns_and_names_them(self):
        # The live finding: the installer's own target had none of the four.
        self._settings({})
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("4 NOT registered", out["detail"])
        self.assertIn("PreCompact", out["detail"], "name what is missing, not just a count")

    def test_partial_registration_names_only_the_missing(self):
        h = self._all_registered()
        del h["PreCompact"]
        self._settings(h)
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 NOT registered", out["detail"])
        self.assertNotIn("Stop:", out["detail"], "a registered hook must not be reported missing")

    def test_registered_but_pointing_at_ANOTHER_checkout_warns(self):
        # The failure that looks healthiest: present, so an existence check passes,
        # but aimed at a stale copy — this host ran a 5-day-old script for days.
        self._settings(self._all_registered(repo_path="/somewhere/else/sutando"))
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("another checkout", out["detail"])

    def test_unparseable_HOOKS_array_warns_rather_than_reporting_clean(self):
        # An empty owned-list would otherwise mean "0 missing of 0" -> "ok", i.e. a
        # probe that cannot fail. Fail toward noise instead.
        (self.repo / "src" / "install-claude-hooks.sh").write_text("#!/usr/bin/env bash\necho hi\n")
        self._settings({})
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("could not parse", out["detail"])

    def test_missing_settings_file_warns(self):
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "warn")
        self.assertIn("never run", out["detail"])

    def test_malformed_settings_warns_never_raises(self):
        (self.repo / ".claude" / "settings.json").write_text("{not json")
        try:
            out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        except Exception as e:
            self.fail(f"must not propagate: {e!r}")
        self.assertEqual(out["status"], "warn")

    def test_not_a_sutando_checkout_is_ok_not_a_warning(self):
        (self.repo / "src" / "install-claude-hooks.sh").unlink()
        out = self.hc.check_claude_hook_registration(repo_dir=self.repo)
        self.assertEqual(out["status"], "ok")

    def test_probe_is_wired_into_run_all_checks(self):
        # Reachability: a probe defined but never called is invisible.
        names = [c.get("name") for c in self.hc.run_all_checks() if isinstance(c, dict)]
        self.assertIn("claude-hooks", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
