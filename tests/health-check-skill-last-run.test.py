#!/usr/bin/env python3
"""src/health-check.py check_skill_last_runs — a skill's declared `last_run` record as a health row.

The manifest names a record under <workspace>/state/; health-check shows it as `skill:<name>` and
names no skill itself.

Run: python3 tests/health-check-skill-last-run.test.py
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent


def _health_check():
    spec = importlib.util.spec_from_file_location("hc_for_skill_last_run", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HC = _health_check()


class SkillLastRun(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        self.skills = Path(tmp.name) / "skills"
        (self.ws / "state").mkdir(parents=True)

    def declare(self, name, rel, **extra):
        (self.skills / name).mkdir(parents=True)
        (self.skills / name / "manifest.json").write_text(json.dumps({"name": name, "last_run": rel, **extra}))

    def record(self, rel, value):
        path = self.ws / "state" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def rows(self):
        return HC.check_skill_last_runs(self.ws, roots=[self.skills])

    def test_the_record_becomes_a_row_named_for_its_skill(self):
        self.declare("demo", "demo/last.json")
        self.record("demo/last.json", {"status": "warn", "detail": "step two failed", "at": "2026-11-21T00:00:00Z"})
        self.assertEqual(self.rows(), [{"name": "skill:demo", "status": "warn",
                                        "detail": "step two failed (at 2026-11-21T00:00:00Z)"}])

    def test_no_record_or_a_non_object_is_no_row(self):
        self.declare("none", "none/last.json")
        self.declare("list", "list/last.json")
        self.declare("junk", "junk/last.json")
        self.record("list/last.json", [1, 2])
        self.record("junk/last.json", "{not json")
        self.assertEqual(self.rows(), [])

    def test_an_object_without_a_valid_status_is_a_warn_naming_the_record(self):
        self.declare("demo", "demo/last.json")
        self.record("demo/last.json", {"status": "down", "detail": "x"})
        (row,) = self.rows()
        self.assertEqual((row["status"], row["detail"]), ("warn", "state/demo/last.json has no ok/warn status"))

    def test_a_record_outside_state_or_not_json_is_never_read(self):
        self.declare("escape", "../secret.json")
        self.declare("text", "text/last.txt")
        (self.ws / "secret.json").write_text(json.dumps({"status": "ok", "detail": "leaked"}))
        self.record("text/last.txt", {"status": "ok"})
        self.assertEqual(self.rows(), [])

    def test_a_disabled_skill_has_no_row_and_the_detail_is_capped(self):
        self.declare("off", "off/last.json", enabled=False)
        self.record("off/last.json", {"status": "ok"})
        self.declare("long", "long/last.json")
        self.record("long/last.json", {"status": "ok", "detail": "x" * 1000})
        (row,) = self.rows()
        self.assertEqual(row["name"], "skill:long")
        self.assertEqual(len(row["detail"]), HC.SKILL_LAST_RUN_DETAIL_CAP)

    def test_health_check_names_no_skill_for_these_rows(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertNotIn("tutorial-page", src)
        self.assertNotIn("check_tutorial_last_run", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
