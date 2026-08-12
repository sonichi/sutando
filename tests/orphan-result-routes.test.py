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

    def routes(self, known=(), limit=orr.DEFAULT_LIMIT):
        return orr.orphan_result_routes(
            self.results, self.tasks, self.archive, known, snowflake_ok, limit
        )

    def _result(self, task_id="task-newsradar-1"):
        (self.results / f"{task_id}.txt").write_text("the answer")
        return task_id

    # --- the reported failure ------------------------------------------

    def test_declared_route_is_recovered(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_text(task_text())
        self.assertEqual(self.routes(), {tid: SNOWFLAKE})

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

    def test_the_scan_is_bounded(self):
        for i in range(5):
            tid = self._result(f"task-{i}")
            (self.tasks / f"{tid}.txt").write_text(task_text())
        self.assertEqual(len(self.routes(limit=2)), 2)

    def test_an_undecodable_task_file_is_skipped_not_fatal(self):
        tid = self._result()
        (self.tasks / f"{tid}.txt").write_bytes(b"source: x\n\xff\xfetask: hi\n")
        self.assertEqual(self.routes(), {})

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
