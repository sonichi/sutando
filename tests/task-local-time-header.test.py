#!/usr/bin/env python3
"""`local_time:` — the owner's wall clock and IANA zone beside the UTC `timestamp:`.

The helper must format a fixed instant exactly, resolve the host zone from
`TZ` or the `/etc/localtime` symlink, and fall back to the bare offset. The
shipped writers must emit the header above `task:`, which stays the body line.

Run: python3 tests/task-local-time-header.test.py
"""
import glob
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import local_task_protocol as ltp  # noqa: E402

INSTANT = datetime(2026, 10, 8, 20, 35, 44, 123456, tzinfo=timezone.utc)
OFFSET_ONLY = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")


def above_task(text, key):
    lines = text.split("\n")
    body = next((i for i, ln in enumerate(lines) if ln.startswith("task:")), len(lines))
    return any(ln.startswith(f"{key}:") for ln in lines[:body])


class Format(unittest.TestCase):
    def test_fixed_zone_summer(self):
        self.assertEqual(ltp.local_time_value(INSTANT, "America/Los_Angeles"),
                         "2026-10-08T13:35:44-07:00 America/Los_Angeles")

    def test_fixed_zone_winter_and_east_of_utc(self):
        winter = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(ltp.local_time_value(winter, "America/Los_Angeles"),
                         "2026-12-01T04:00:00-08:00 America/Los_Angeles")
        self.assertEqual(ltp.local_time_value(INSTANT, "Asia/Kolkata"),
                         "2026-10-09T02:05:44+05:30 Asia/Kolkata")

    def test_no_zone_falls_back_to_offset_only(self):
        with mock.patch.object(ltp, "host_zone_name", return_value=None):
            self.assertRegex(ltp.local_time_value(INSTANT), OFFSET_ONLY)

    def test_unknown_zone_falls_back_to_offset_only(self):
        self.assertRegex(ltp.local_time_value(INSTANT, "Nowhere/Atlantis"), OFFSET_ONLY)

    def test_key_is_registered_in_both_copies(self):
        from ag2_sparrow import local_task_protocol as vendored
        self.assertIn("local_time", ltp.KNOWN_HEADER_KEYS)
        self.assertIn("local_time", vendored.KNOWN_HEADER_KEYS)


class HostZone(unittest.TestCase):
    def _link(self, target, zdir_name="zoneinfo"):
        d = tempfile.mkdtemp()
        zdir = Path(d, zdir_name, *target.split("/")[:-1])
        zdir.mkdir(parents=True)
        real = zdir / target.split("/")[-1]
        real.write_text("")
        link = Path(d, "localtime")
        link.symlink_to(real)
        return str(link)

    def test_symlink_names_the_zone(self):
        with mock.patch.dict(os.environ, {"TZ": ""}):
            self.assertEqual(ltp.host_zone_name(self._link("Asia/Tokyo")), "Asia/Tokyo")

    def test_macos_default_zoneinfo_dir_names_the_zone(self):
        with mock.patch.dict(os.environ, {"TZ": ""}):
            link = self._link("America/Los_Angeles", "zoneinfo.default")
            self.assertEqual(ltp.host_zone_name(link), "America/Los_Angeles")

    def test_tz_env_wins(self):
        with mock.patch.dict(os.environ, {"TZ": ":Europe/Paris"}):
            self.assertEqual(ltp.host_zone_name(self._link("Asia/Tokyo")), "Europe/Paris")

    def test_unresolvable_is_none(self):
        with mock.patch.dict(os.environ, {"TZ": ""}):
            self.assertIsNone(ltp.host_zone_name("/nonexistent/localtime"))


class Writers(unittest.TestCase):
    def test_taskify_consumer_emits_it_above_task(self):
        from ag2_sparrow.event_consumer import TaskifyHandler
        d = tempfile.mkdtemp()
        h = TaskifyHandler(task_dir=d, agent_mxid="@me:ag2.space", threshold=1, log=lambda *a: None)
        h.offer({"event_id": "e1", "type": "message.created", "room_id": "!r:ag2.space",
                 "actor_id": "@someone:ag2.space", "cursor": 1, "content": {"body": "hi"}})
        text = Path(glob.glob(os.path.join(d, "*.txt"))[0]).read_text()
        self.assertTrue(above_task(text, "local_time"), text)
        self.assertRegex(text, r"(?m)^timestamp: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("local_time", ltp.parse_task_headers(text).headers)

    def test_gateway_writer_emits_it_above_task(self):
        tmp = Path(tempfile.mkdtemp(prefix="rgb-local-time-"))
        import workspace_default as _wd
        # A placeholder token keeps the import from reading a channel .env or the Keychain.
        hermetic = {"REMOTE_TASK_TOKEN": "https://relay.invalid/relay|placeholder"}
        with mock.patch.dict(os.environ, hermetic), \
                mock.patch.object(_wd, "resolve_workspace", lambda migrate=True: tmp):
            for sub in ("tasks", "results", "state"):
                (tmp / sub).mkdir(parents=True, exist_ok=True)
            spec = importlib.util.spec_from_file_location(
                "remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")
            rgb = importlib.util.module_from_spec(spec)
            sys.modules["remote_gateway_bridge"] = rgb
            spec.loader.exec_module(rgb)
        rgb.TASKS_DIR = tmp / "tasks"
        rgb.RESULTS_DIR = tmp / "results"
        rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
        written = rgb._write_task({"id": "lt-1", "timestamp": "2026-10-08T20:35:44Z",
                                   "task": "what time is it", "channel_id": "!abc:ag2.space"})
        self.assertTrue(written)
        text = (rgb.TASKS_DIR / f"{written[0]}.txt").read_text()
        self.assertIn("timestamp: 2026-10-08T20:35:44Z\n", text)
        self.assertTrue(above_task(text, "local_time"), text)
        self.assertLess(text.index("timestamp:"), text.index("local_time:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
