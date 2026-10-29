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

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import shutil
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

    def test_an_unknown_outcome_park_warns_against_a_blind_requeue(self):
        self._items(".outbox-withheld-review").joinpath("wr_1.c.json").write_text(json.dumps(
            {"item_id": "wr_1.c", "status": "PARKED", "reason": "outcome-unknown"}), encoding="utf-8")
        self._write("task-abc", "PARKED")
        r = self.hc.check_outbox_parked(self.ws)
        self.assertIn("1 may have been delivered — do not requeue without checking the room: wr_1.c",
                      r["detail"])
        self.assertIn("1 never delivered — nothing retries them: task-abc", r["detail"])

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


class WithheldReviewsReadable(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.base = self.ws / "state" / "withheld-team-results"
        (self.base / "conflicts").mkdir(parents=True)

    def test_no_reviews_is_ok(self):
        self.assertEqual(self.hc.check_withheld_reviews_readable(self.ws)["status"], "ok")

    def test_records_that_read_are_ok(self):
        (self.base / "wr_0000000000000001.json").write_text(json.dumps({"status": "awaiting_owner"}))
        self.assertEqual(self.hc.check_withheld_reviews_readable(self.ws)["status"], "ok")

    def test_each_unreadable_record_is_named(self):
        frozen = self.base / "conflicts" / "wr_0000000000000002.abcd.json"
        for content in ("{not json", "[]", '"str"', ""):
            with self.subTest(content=content):
                frozen.write_text(content)
                r = self.hc.check_withheld_reviews_readable(self.ws)
                self.assertEqual(r["status"], "warn")
                self.assertIn("withheld-team-results/conflicts/wr_0000000000000002.abcd.json", r["detail"])
                self.assertIn("please reply again", r["detail"])

    def test_a_symlinked_record_is_named(self):
        (self.base / "archive").mkdir()
        (self.base / "archive" / "real.json").write_text(json.dumps({"status": "kept_private"}))
        (self.base / "archive" / "wr_0000000000000003.json").symlink_to("real.json")
        r = self.hc.check_withheld_reviews_readable(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("archive/wr_0000000000000003.json", r["detail"])

    def test_a_linked_or_odd_review_folder_is_named(self):
        real = self.ws / "elsewhere"
        real.mkdir()
        (real / "wr_0000000000000004.json").write_text(json.dumps({"status": "awaiting_owner"}))
        (self.base / "conflicts").rmdir()
        for label, make in (("linked", lambda f: f.symlink_to(real)),
                            ("dangling", lambda f: f.symlink_to(self.ws / "missing")),
                            ("a file", lambda f: f.write_text("x"))):
            for folder in ("conflicts", "archive"):
                with self.subTest(folder=folder, kind=label):
                    target = self.base / folder
                    make(target)
                    try:
                        r = self.hc.check_withheld_reviews_readable(self.ws)
                    finally:
                        target.unlink()
                    self.assertEqual(r["status"], "warn")
                    self.assertIn(f"state/withheld-team-results/{folder} (", r["detail"])

    def test_it_agrees_with_the_guard(self):
        """A structural delegation pin: the check calls the guard's shared preflight and scans
        nothing itself; the behavioural tests above cover what that preflight reports."""
        source = (REPO / "src" / "health-check.py").read_text()
        body = source[source.index("def check_withheld_reviews_readable"):]
        body = body[:body.index("\ndef ")]
        self.assertTrue("guard.namespace_problems(" in body and "os.listdir" not in body
                        and ".glob(" not in body and "read_text" not in body,
                        "the health check must use the guard's own read-only preflight")

    def test_linked_claims_or_issued_is_named(self):
        for folder in ("claims", "issued"):
            with self.subTest(folder=folder):
                real = self.ws / f"{folder}-real"
                real.mkdir()
                (real / "wr_0000000000000005.c0ffee").write_text("")
                target = self.base / folder
                target.symlink_to(real)
                try:
                    r = self.hc.check_withheld_reviews_readable(self.ws)
                finally:
                    target.unlink()
                self.assertEqual(r["status"], "warn")
                self.assertIn(f"state/withheld-team-results/{folder} (", r["detail"])

    def test_a_linked_entry_inside_claims_or_issued_is_named(self):
        for folder in ("claims", "issued"):
            with self.subTest(folder=folder):
                inside = self.base / folder
                inside.mkdir()
                (self.ws / "elsewhere-file").write_text("")
                (inside / "wr_0000000000000006.entry").symlink_to(self.ws / "elsewhere-file")
                try:
                    r = self.hc.check_withheld_reviews_readable(self.ws)
                finally:
                    shutil.rmtree(inside)
                self.assertEqual(r["status"], "warn")
                self.assertIn(f"{folder}/wr_0000000000000006.entry", r["detail"])

    def test_a_directory_or_fifo_entry_in_claims_or_issued_is_named(self):
        for folder in ("claims", "issued"):
            for kind in ("directory", "fifo"):
                with self.subTest(folder=folder, kind=kind):
                    inside = self.base / folder
                    inside.mkdir()
                    entry = inside / "wr_0000000000000007.odd"
                    entry.mkdir() if kind == "directory" else os.mkfifo(entry)
                    try:
                        r = self.hc.check_withheld_reviews_readable(self.ws)
                    finally:
                        shutil.rmtree(inside)
                    self.assertEqual(r["status"], "warn")
                    self.assertIn(f"{folder}/wr_0000000000000007.odd", r["detail"])

    def test_run_all_checks_registers_the_row(self):
        sentinel = {"name": "withheld-reviews-readable", "status": "ok",
                    "detail": "registration sentinel"}
        with unittest.mock.patch.object(self.hc, "check_withheld_reviews_readable",
                                        return_value=sentinel) as probe:
            checks = self.hc.run_all_checks()
        probe.assert_called_once_with()
        self.assertIn(sentinel, checks)


if __name__ == "__main__":
    unittest.main()
