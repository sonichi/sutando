#!/usr/bin/env python3
"""A PARKED outbound item is a reply the owner never received, and nothing
retries it — `outbox_cli`'s header calls PARKED a durable terminal state that
nothing in production could lift. Before this probe it was visible only to
whoever ran the CLI by hand; a reply to @rui sat undelivered 45h that way.

THE TRAP THIS PINS, at THREE levels. `outbox.list_items` swallows per-file
OSErrors and globs an unreadable directory to `[]`, so its empty list cannot
tell clean from unjudgeable. The same blindness reaches one and two directories
up: `Path.glob` answers `[]` for an unreadable `results/` (a reassuring 0), and
`Path.is_dir()` PROPAGATES EACCES rather than answering False, so an unreadable
`.outbox/` root made the probe RAISE — and `run_all_checks()` wraps no check, so
a raise there aborts every probe after it. Reassuring zero and aborted run are
both failures this file exists to prevent, and the chmod cases skip under root
and on Windows, so each is also covered by an injected OSError that does not.

Run: python3 tests/health-check-outbox-parked.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
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

    def _items(self, root_name=".outbox"):
        d = self.ws / "results" / root_name / ".items"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _require_modes_enforced(self):
        # A 0 mode denies nothing under root or on Windows, so these cases would
        # pass vacuously there; the injection tests below cover both.
        if sys.platform == "win32":
            self.skipTest("directory modes do not deny traversal on Windows")
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")

    def _raise_on(self, target, exc):
        real = Path.iterdir
        def fake(self_path):
            if Path(self_path) == Path(target):
                raise exc
            return real(self_path)
        patcher = unittest.mock.patch.object(Path, "iterdir", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, item_id, status, root_name=".outbox"):
        self._items(root_name).joinpath(f"{item_id}.json").write_text(
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
        self._require_modes_enforced()
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
        self._require_modes_enforced()
        import outbox
        self._write("task-abc", "PARKED")
        items = self._items()
        os.chmod(items, 0)
        self.addCleanup(os.chmod, items, 0o755)
        self.assertEqual(outbox.list_items(self.ws / "results" / ".outbox", status="PARKED"), [])

    def test_an_unreadable_outbox_ROOT_is_unjudged_not_a_clean_zero(self):
        # One level above the case above: the guard used to be skipped here and
        # the probe raised, which aborts every later check in run_all_checks().
        self._require_modes_enforced()
        self._write("task-abc", "PARKED")
        root = self.ws / "results" / ".outbox"
        os.chmod(root, 0)
        self.addCleanup(os.chmod, root, 0o755)
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_an_unreadable_results_dir_is_unjudged_not_a_clean_zero(self):
        self._require_modes_enforced()
        self._write("task-abc", "PARKED")
        os.chmod(self.ws / "results", 0)
        self.addCleanup(os.chmod, self.ws / "results", 0o755)
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_a_root_without_an_items_dir_is_empty_not_unreadable(self):
        (self.ws / "results" / ".outbox").mkdir(parents=True)
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "ok")

    def test_an_injected_EACCES_on_results_is_unjudged_on_every_platform(self):
        # Runs where chmod cannot deny (root, Windows), so the guard is never
        # merely skipped in CI.
        self._write("task-abc", "PARKED")
        self._raise_on(self.ws / "results", PermissionError(13, "Permission denied"))
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_an_injected_EACCES_on_the_items_dir_is_unjudged_on_every_platform(self):
        self._write("task-abc", "PARKED")
        self._raise_on(self.ws / "results" / ".outbox" / ".items",
                       PermissionError(13, "Permission denied"))
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_the_probe_never_raises_because_a_raise_aborts_later_checks(self):
        self._write("task-abc", "PARKED")
        self._raise_on(self.ws / "results", OSError(5, "I/O error"))
        try:
            r = self.hc.check_outbox_parked(self.ws)
        except OSError as exc:
            self.fail(f"probe raised {exc!r}; run_all_checks has no guard, so "
                      "every later check would be aborted")
        # "warn" alone would also be satisfied by the PARKED branch, which is
        # how this passed against the unfixed probe; the reason must match.
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_is_dir_never_answers_True_under_an_unreadable_parent(self):
        """The premise of the ROOT case, in the only form that is version-agnostic.

        Which way it fails moved in CPython: <=3.13 raises, 3.14 answers False.
        Either way it cannot report a readable dir, which is why the probe uses
        iterdir() instead of asking this question at all.
        """
        self._require_modes_enforced()
        parent = self.ws / "results" / ".outbox"
        (parent / ".items").mkdir(parents=True)
        os.chmod(parent, 0)
        self.addCleanup(os.chmod, parent, 0o755)
        try:
            answer = (parent / ".items").is_dir()
        except OSError:
            return
        self.assertFalse(answer, "is_dir() claimed an unreadable dir is readable")

    def test_an_unimportable_outbox_module_is_unjudged_not_a_clean_zero(self):
        # The last uncovered branch: without a reader there is no way to judge a
        # root, and a clean zero would hide whatever is parked in it.
        self._write("task-abc", "PARKED")
        with unittest.mock.patch.dict(sys.modules, {"outbox": None}):
            r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_a_parked_item_in_a_non_default_root_names_THAT_root_not_dot_outbox(self):
        # Regression: the recovery hint used to hardcode `.outbox` for every root.
        self._write("proactive-telegram-bridge-restarted-1.txt#1", "PARKED",
                    root_name=".outbox-discord-proactive")
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn(".outbox-discord-proactive", r["detail"])
        self.assertIn("proactive-telegram-bridge-restarted-1.txt#1", r["detail"])

    def test_parked_items_across_two_roots_are_both_counted_with_their_own_root(self):
        self._write("task-a", "PARKED", root_name=".outbox")
        self._write("task-b", "PARKED", root_name=".outbox-discord-proactive")
        r = self.hc.check_outbox_parked(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("2 repl", r["detail"])
        self.assertIn("task-a", r["detail"])
        self.assertIn("task-b", r["detail"])
        self.assertIn(".outbox-discord-proactive", r["detail"])

    def test_the_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text(encoding="utf-8")
        self.assertIn("checks.append(check_outbox_parked())", src)


if __name__ == "__main__":
    unittest.main()
