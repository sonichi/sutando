#!/usr/bin/env python3
"""seat_inbox: an inbox is a seat only when the workspace's records register it.

Run: python3 tests/seat-inbox.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import seat_inbox  # noqa: E402

W = "0123456789abcdef"


class SeatInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        for d in ("tasks", "state", f"deliveries/{W}", "deliveries/gone", "deliveries/stranger"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {W: {"state": "idle"}, "gone": {"state": "retired"}, "own-one": {"state": "idle"}}}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seat(self, inbox, homes=()):
        with mock.patch("connections.own_worker_homes", return_value=list(homes)):
            return seat_inbox.seat(inbox, self.ws)

    def test_the_core_inbox_is_the_core(self):
        self.assertEqual(self.seat(self.ws / "tasks"), {"seat": "core", "worker": "", "label": "the core"})

    def test_the_core_inbox_through_a_symlink_is_still_the_core(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.ws)
        self.assertEqual(self.seat(link / "tasks")["seat"], "core")

    def test_a_rostered_worker_delivery_inbox_is_that_worker(self):
        self.assertEqual(self.seat(self.ws / "deliveries" / W),
                         {"seat": "worker", "worker": W, "label": "worker 01234567"})

    def test_a_retired_or_unrostered_delivery_inbox_is_no_seat(self):
        for wid in ("gone", "stranger"):
            self.assertEqual(self.seat(self.ws / "deliveries" / wid)["seat"], "", wid)

    def test_a_rostered_worker_own_tasks_folder_is_that_worker(self):
        own = self.ws / "agents" / "own" / "tasks"
        own.mkdir(parents=True)
        homes = [{"worker": "own-one", "tasks": own, "results": own.parent / "results"}]
        self.assertEqual(self.seat(own, homes)["label"], "worker own-one")
        unrostered = [{"worker": "nobody", "tasks": own, "results": own.parent / "results"}]
        self.assertEqual(self.seat(own, unrostered)["seat"], "")

    def test_a_scratch_inbox_is_no_seat_even_when_its_parent_looks_like_a_workspace(self):
        scratch = Path(self.tmp.name) / "scratch" / "tasks"
        scratch.mkdir(parents=True)
        self.assertEqual(self.seat(scratch), seat_inbox.NONE)

    def test_an_unreadable_roster_registers_no_worker_but_keeps_the_core(self):
        (self.ws / "state" / "roster.json").write_text("{not json")
        self.assertEqual(self.seat(self.ws / "deliveries" / W)["seat"], "")
        self.assertEqual(self.seat(self.ws / "tasks")["seat"], "core")

    def test_a_roster_whose_workers_is_not_a_map_registers_no_worker(self):
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": [W]}))
        self.assertEqual(self.seat(self.ws / "deliveries" / W), seat_inbox.NONE)

    def test_no_inbox_or_no_workspace_is_no_seat(self):
        self.assertEqual(seat_inbox.seat("", self.ws), seat_inbox.NONE)
        self.assertEqual(seat_inbox.seat(self.ws / "tasks", None), seat_inbox.NONE)

    def test_an_unreadable_connections_record_registers_no_own_folder(self):
        own = self.ws / "agents" / "own" / "tasks"
        own.mkdir(parents=True)
        with mock.patch("connections.own_worker_homes", side_effect=OSError("unreadable")):
            self.assertEqual(seat_inbox.seat(own, self.ws), seat_inbox.NONE)
            self.assertEqual(seat_inbox.seat(self.ws / "deliveries" / W, self.ws)["seat"], "worker")

    def test_cli_without_a_workspace_uses_the_resolved_one(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("workspace_default.resolve_workspace", return_value=self.ws):
            seat_inbox.main(["seat", "--inbox", str(self.ws / "tasks")])
        self.assertEqual(buf.getvalue().strip(), "core\t\tthe core")

    def test_cli_prints_tab_separated_or_none(self):
        import io
        from contextlib import redirect_stdout
        for inbox, want in ((self.ws / "deliveries" / W, f"worker\t{W}\tworker 01234567"),
                            (Path(self.tmp.name), "none")):
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("connections.own_worker_homes", return_value=[]):
                seat_inbox.main(["seat", "--inbox", str(inbox), "--workspace", str(self.ws)])
            self.assertEqual(buf.getvalue().strip(), want)


if __name__ == "__main__":
    unittest.main()
