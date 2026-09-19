#!/usr/bin/env python3
"""A PARKED outbound item is a reply the owner never received, and nothing
retries it — `outbox_cli`'s header calls PARKED a durable terminal state that
nothing in production could lift. Before this probe it was visible only to
whoever ran the CLI by hand; a reply to @rui sat undelivered 45h that way.

THE TRAP THIS PINS. `outbox.list_items` swallows per-file OSErrors and globs an
unreadable directory to `[]`, so its empty list cannot tell clean from
unjudgeable. A probe that trusted it would report a reassuring 0 over an
unreadable outbox, which is the failure this file exists to prevent.

Run: python3 tests/health-check-outbox-parked.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_outbox_parked_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(hc)
    except SystemExit:
        pass
    return hc


class OutboxParkedProbe(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "results").mkdir()

    def _items(self):
        d = self.ws / "results" / ".outbox" / ".items"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write(self, item_id, status):
        self._items().joinpath(f"{item_id}.json").write_text(
            json.dumps({"item_id": item_id, "status": status}), encoding="utf-8")

    def test_no_outbox_root_says_its_zero_is_untestable(self):
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("untestable", r["detail"])

    def test_a_root_with_nothing_parked_is_ok(self):
        self._items()
        self.assertEqual(self.hc.check_outbox_parked(self.ws)["status"], "ok")

    def test_a_parked_item_warns_and_names_it(self):
        self._write("task-abc", "PARKED")
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("task-abc", r["detail"])
        self.assertIn("requeue", r["detail"])

    def test_a_delivered_item_is_not_counted(self):
        self._write("task-abc", "PARKED")
        self._write("task-def", "DELIVERED")
        r = self.hc.check_outbox_parked(self.ws)
        self.assertIn("1 reply", r["detail"])
        self.assertNotIn("task-def", r["detail"])

    def test_an_unreadable_root_is_unjudged_never_a_clean_zero(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        self._write("task-abc", "PARKED")
        items = self._items()
        os.chmod(items, 0)
        self.addCleanup(os.chmod, items, 0o755)
        r = self.hc.check_outbox_parked(self.ws)
        # `list_items` would return [] here; reporting ok would hide the backlog.
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_list_items_really_does_hide_an_unreadable_dir(self):
        """The premise of the test above, asserted rather than assumed."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        import outbox
        self._write("task-abc", "PARKED")
        items = self._items()
        os.chmod(items, 0)
        self.addCleanup(os.chmod, items, 0o755)
        self.assertEqual(outbox.list_items(self.ws / "results" / ".outbox", status="PARKED"), [])

    def test_the_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text(encoding="utf-8")
        self.assertIn("checks.append(check_outbox_parked())", src)


if __name__ == "__main__":
    unittest.main()
