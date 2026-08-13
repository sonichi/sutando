#!/usr/bin/env python3
"""A result nobody delivers errors nowhere, so every case here is a silent one.

Run: python3 tests/orphan-result-routes.test.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# This file reads discord-bridge.py as TEXT and never imports it, so no host
# config is resolved — isolated anyway, so it stays true if that ever changes.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="orphan-routes-hermetic-")
_ccd = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_ccd.mkdir(parents=True, exist_ok=True)
(_ccd / "access.json").write_text('{"allowFrom": [], "groups": {}}')

import task_archive as _task_archive  # noqa: E402  (after the sys.path insert)

spec = importlib.util.spec_from_file_location(
    "orphan_result_routes", REPO / "src" / "orphan_result_routes.py"
)
orr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orr)

SNOWFLAKE = "1509379092116672602"


def snowflake_ok(value: str) -> bool:
    return value.isdigit() and 17 <= len(value) <= 20


def task_text(channel_id: str | None = SNOWFLAKE, source: str = "news-radar",
              body: str = "do the thing") -> str:
    lines = ["id: task-1", f"source: {source}"]
    if channel_id is not None:
        lines.append(f"channel_id: {channel_id}")
    lines += ["access_tier: owner", f"task: {body}"]
    return "\n".join(lines) + "\n"


class OrphanResultRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.results = root / "results"
        self.tasks = root / "tasks"
        self.archive = self.tasks / "archive"
        for d in (self.results, self.tasks, self.archive):
            d.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, known=(), limit=orr.DEFAULT_LIMIT, cursor=""):
        return orr.orphan_result_routes(
            self.results, self.tasks, known, snowflake_ok, limit, cursor
        )

    def routes(self, known=(), limit=orr.DEFAULT_LIMIT, cursor=""):
        return self.call(known, limit, cursor)[0]

    def _result(self, task_id="task-newsradar-1"):
        (self.results / f"{task_id}.txt").write_text("the answer")
        return task_id

    # --- the reported failure ------------------------------------------

    def test_declared_route_is_recovered(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    def test_a_quarantined_task_still_yields_its_route(self):
        """A failed archive must not also strand the reply.

        archive_file() mints <id>.txt.archive-failed when it cannot archive;
        that file is then the task's only surviving header block, so routing
        has to see it or the result is undeliverable forever.
        """
        tid = self._result()
        (self.tasks / f"{tid}.txt.archive-failed").write_text(task_text())
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    def test_a_suffixed_quarantine_still_yields_its_route(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt.archive-failed.1").write_text(task_text())
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    def test_a_live_task_outranks_a_quarantined_leftover(self):
        """Precedence matters: the quarantined copy can be older, and routing
        the stale channel_id would post the reply to the wrong place."""
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        (self.tasks / f"{tid}.txt.archive-failed").write_text(
            task_text().replace(SNOWFLAKE, "9" * 19))
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    def test_a_quarantine_for_another_id_is_not_matched(self):
        """The lookup globs, so a longer id sharing this one's prefix must not
        satisfy it — that would route one task's reply using another's headers."""
        tid = self._result()
        (self.tasks / f"{tid}-extra.txt.archive-failed").write_text(task_text())
        self.assertEqual(self.routes(), {})

    def test_archived_task_still_yields_its_route(self):
        # By the time a result exists the core has usually archived the task,
        # so an archive miss would make the fix work only in a race.
        tid = self._result()
        month = self.archive / "2026-08"
        month.mkdir()
        (month / f"{tid}.txt").write_text(task_text())
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    def test_a_claimed_task_file_is_found(self):
        # find_task_file's shape: a claimed task is `<id>.claimed-core-N.txt`.
        tid = self._result()
        (self.tasks / f"{tid}.claimed-core-1.txt").write_text(task_text())
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    # --- must not fire --------------------------------------------------

    def test_an_id_the_bridge_already_tracks_is_left_alone(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        self.assertEqual(self.routes(known={tid}), {})

    def test_no_task_file_means_no_route(self):
        # Guessing a destination here would post a private body somewhere.
        self._result()
        self.assertEqual(self.routes(), {})

    def test_a_foreign_transport_is_never_adopted(self):
        # A Telegram/Matrix task can carry a numeric id that passes a snowflake
        # shape check; the declared source is what rules it out.
        for source in sorted(orr.FOREIGN_SOURCES):
            with self.subTest(source=source):
                tid = self._result(f"task-{source}-1")
                (self.tasks / f"{tid}.txt").write_text(task_text(source=source))
                self.assertNotIn(tid, self.routes())

    def test_foreign_source_matching_is_case_insensitive(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text(source="Telegram"))
        self.assertEqual(self.routes(), {})

    def test_a_non_snowflake_channel_id_is_rejected(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text(channel_id="local-chat"))
        self.assertEqual(self.routes(), {})

    def test_a_missing_channel_id_is_not_invented(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text(channel_id=None))
        self.assertEqual(self.routes(), {})

    def test_an_empty_channel_id_is_not_a_route(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text(channel_id=""))
        self.assertEqual(self.routes(), {})

    def test_the_body_cannot_forge_a_route(self):
        # The whole reason for the task-last parser: everything from `task:`
        # onward is attacker-controlled and must not become metadata.
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(
            task_text(channel_id=None, body=f"hi\nchannel_id: {SNOWFLAKE}")
        )
        self.assertEqual(self.routes(), {})

    def test_a_body_cannot_override_a_real_header(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(
            task_text(body=f"hi\nchannel_id: {'9' * 18}")
        )
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

    def test_non_task_files_in_results_are_ignored(self):
        (self.results / "proactive-1.txt").write_text("body")
        (self.results / "task-1.sending").write_text("claimed")
        (self.tasks / "proactive-1.txt").write_text(task_text())
        self.assertEqual(self.routes(), {})

    # --- failure modes must not read as "nothing to route" --------------

    def test_an_empty_results_dir_returns_a_reset_cursor(self):
        # results/ exists and is empty — distinct from the unreadable case, and
        # the only path that returns the cursor reset without examining anything.
        routes, cursor = self.call(cursor="task-anything.txt")
        self.assertEqual((routes, cursor), ({}, ""))

    def test_the_scan_is_bounded(self):
        for i in range(5):
            tid = self._result(f"task-{i}")
            (self.tasks / f"{tid}.txt").write_text(task_text())
        self.assertEqual(len(self.routes(limit=2)), 2)

    def test_the_bound_is_on_WORK_examined_not_routes_returned(self):
        # 100 unroutable results with limit=1 used to run 100 task lookups per
        # tick, each able to walk the archive — and poll_results runs every 1s.
        for i in range(100):
            self._result(f"task-unroutable-{i:03d}")
        calls = []
        real_a, real_f = orr.find_archived_task, orr.find_task_file
        orr.find_archived_task = lambda d, i: (calls.append(i), real_a(d, i))[1]
        orr.find_task_file = lambda d, i: (calls.append(i), real_f(d, i))[1]
        try:
            routes, _ = self.call(limit=1)
        finally:
            orr.find_archived_task, orr.find_task_file = real_a, real_f
        self.assertEqual(routes, {})
        self.assertLessEqual(len(calls), 2, f"{len(calls)} lookups for limit=1 (<=2: one per helper)")

    def test_the_cursor_advances_so_nothing_starves(self):
        # A permanently-unroutable prefix must not hide a routable entry behind it.
        for i in range(3):
            self._result(f"task-a-unroutable-{i}")
        tid = self._result("task-z-routable")
        (self.tasks / f"{tid}.txt").write_text(task_text())
        seen, cursor = {}, ""
        for _ in range(6):
            routes, cursor = self.call(limit=1, cursor=cursor)
            seen.update(routes)
        self.assertEqual(seen, {tid: SNOWFLAKE}, "round-robin never reached the routable entry")

    def test_the_cursor_resets_at_the_end_of_the_listing(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        _, cursor = self.call(limit=99)
        self.assertEqual(cursor, "", "a full pass must wrap, not pin the cursor at the last name")

    def test_an_undecodable_task_file_is_skipped_not_fatal(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_bytes(b"source: x\n\xff\xfetask: hi\n")
        self.assertEqual(self.routes(), {})

    def test_a_RAISING_lookup_skips_the_item_instead_of_killing_the_poll(self):
        # The chmod test below cannot carry this on its own: Path.exists()
        # delegates to os.path.exists on 3.14 (returns False) but calls stat()
        # directly on 3.12 (raises EACCES), so the realistic fixture passes
        # vacuously on a newer local interpreter and only fails on CI.
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        real = orr.find_archived_task
        orr.find_archived_task = lambda d, i: (_ for _ in ()).throw(
            PermissionError(13, "Permission denied"))
        orr.find_task_file, real_f = (lambda d, i: None), orr.find_task_file
        try:
            self.assertEqual(self.routes(), {})
        finally:
            orr.find_archived_task, orr.find_task_file = real, real_f

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_an_unreadable_archive_skips_the_item_without_raising(self):
        tid = self._result()
        month = self.archive / "2026-08"
        month.mkdir()
        (month / f"{tid}.txt").write_text(task_text())
        os.chmod(self.archive, 0o000)
        try:
            self.assertEqual(self.routes(), {})
        finally:
            os.chmod(self.archive, 0o755)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_an_unreadable_results_dir_returns_empty_without_raising(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        os.chmod(self.results, 0o000)
        try:
            self.assertEqual(self.routes(), {})
        finally:
            os.chmod(self.results, 0o755)


class WiringTest(unittest.TestCase):
    """Structural, per this repo's existing bridge-test convention: importing
    discord-bridge pulls in discord.py and reads env, so the call is asserted."""

    src = (REPO / "src" / "discord-bridge.py").read_text()

    def test_bridge_imports_the_shared_resolver(self):
        self.assertIn("from orphan_result_routes import orphan_result_routes", self.src)

    def test_poll_results_calls_it(self):
        body = self.src.split("async def poll_results(")[1]
        self.assertIn("orphan_result_routes(", body,
                      "poll_results never calls it, so nothing adopts an orphan route")

    def test_delivery_cleanup_archives_a_CLAIMED_task_not_a_rebuilt_bare_name(self):
        # ONE owner for the archive-then-clear policy. Asserting the resolver
        # inside it beats matching call sites: a new site inherits it for free.
        m = re.search(r"def _archive_delivered_pair\(.*?\n\n\n", self.src, re.S)
        self.assertIsNotNone(m, "the shared post-delivery cleanup helper is missing")
        helper = m.group(0)
        expr = re.search(r"task_file = ([^\n]*)\n", helper)
        self.assertIsNotNone(expr, "the helper must resolve a task path")
        with tempfile.TemporaryDirectory() as td:
            tasks = Path(td)
            claimed = tasks / "task-99.claimed-core-1.txt"
            claimed.write_text("body")
            ns = {"find_task_file": _task_archive.find_task_file,
                  "TASKS_DIR": tasks, "task_id": "task-99"}
            self.assertEqual(eval(expr.group(1), ns), claimed,
                             "cleanup resolves the bare name, not the claimed file")
        # and every delivery path must go THROUGH it rather than open-coding
        self.assertGreaterEqual(
            len(re.findall(r"^\s+_archive_delivered_pair\(result_file, task_id\)$",
                           self.src, re.M)), 2,
            "both delivery paths must call the shared helper")
        self.assertNotIn("_gone = archive_file", self.src,
                         "an open-coded cleanup site bypasses the shared policy")

    def test_the_bridge_injects_a_snowflake_validator(self):
        m = re.search(r"def _is_discord_channel_id\(value: str\) -> bool:.*?return ([^\n]+)",
                      self.src, re.S)
        self.assertIsNotNone(m, "the bridge must supply its own id validator")
        ns: dict = {}
        exec(f"def f(value):\n    return {m.group(1)}", ns)
        self.assertTrue(ns["f"](SNOWFLAKE))
        self.assertFalse(ns["f"]("123456789"))          # a Telegram chat id
        self.assertFalse(ns["f"]("!room:ag2.space"))    # a Matrix room id
        self.assertFalse(ns["f"]("C09ABCDEF"))          # a Slack channel id


if __name__ == "__main__":
    unittest.main(verbosity=2)
