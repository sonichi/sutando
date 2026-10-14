#!/usr/bin/env python3
"""src/ag2space_links.py: the shapes of the links that open AG2 Space inside the app, each
caller's delegation to it, and no other link builder in the tree."""
import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO / "skills" / "pending-questions" / "scripts")]
import ag2space_links as links  # noqa: E402


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cron = _load("cron_notify_links", "skills/schedule-crons/cron-notify.py")
pq = _load("pq_room_db_links", "skills/pending-questions/scripts/pending_questions_room_db.py")


class TestShapes(unittest.TestCase):
    def test_room_and_event_links_are_matrix_to_permalinks(self):
        self.assertEqual(links.room_link("!r:ag2.space"), "https://matrix.to/#/!r:ag2.space?via=ag2.space")
        self.assertEqual(links.room_link("!r:ag2.space", "$e"), "https://matrix.to/#/!r:ag2.space/$e?via=ag2.space")
        self.assertEqual(links.room_link("!r:ag2.space", via=""), "https://matrix.to/#/!r:ag2.space")

    def test_a_surface_link_is_the_home_route_with_the_room_encoded(self):
        self.assertEqual(links.surface_link("https://chat.ag2.space/", "!abc:ag2.space", "doc", "readme"),
                         "https://chat.ag2.space/home/!abc%3Aag2.space?surface=doc&page=readme")
        self.assertEqual(links.surface_link("https://chat.ag2.space/relay", "#a/b:ag2.space", "database"),
                         "https://chat.ag2.space/home/%23a%2Fb%3Aag2.space?surface=database")

    def test_a_surface_link_is_never_the_hash_room_route(self):
        self.assertNotIn("#/room/", links.surface_link("https://chat.ag2.space", "!abc:ag2.space", "database"))

    def test_a_database_page_rides_only_in_the_shape_the_app_reads(self):
        self.assertEqual(links.database_link("https://x.test", "!r:x.test", "d1ab"),
                         "https://x.test/home/!r%3Ax.test?surface=database&page=d1ab")
        self.assertEqual(links.database_link("https://x.test", "!r:x.test", "pendingq"),
                         "https://x.test/home/!r%3Ax.test?surface=database&page=pendingq")
        for bad in ("a|b", "-x", "a/b", "x" * 65):
            self.assertEqual(links.database_link("https://x.test", "!r:x.test", bad),
                             "https://x.test/home/!r%3Ax.test?surface=database", bad)

    def test_an_unusable_origin_or_room_is_refused(self):
        for origin, room in (("chat.ag2.space", "!r:x"), ("https://x.test", "r:x"), ("https://x.test", "!r")):
            with self.assertRaises(ValueError):
                links.surface_link(origin, room, "database")


class TestCallersDelegate(unittest.TestCase):
    def test_cron_notify_deep_link_is_room_link(self):
        with mock.patch.object(cron, "room_link", return_value="L") as rl:
            self.assertEqual(cron.deep_link("!r:x", "$e", "v"), "L")
        rl.assert_called_once_with("!r:x", "$e", "v")
        self.assertIn(links.room_link("!r:x", "$e"), cron.format_ping("c", "s", "!r:x", "$e"))

    def test_pending_questions_link_is_database_link(self):
        with mock.patch.object(pq, "database_link", return_value="L") as dl:
            self.assertEqual(pq.db_link("https://x.test", "!r:x", "pendingq"), "L")
        dl.assert_called_once_with("https://x.test", "!r:x", "pendingq")
        self.assertIsNone(pq.db_link("not-a-url", "!r:x", "pendingq"))

    def test_no_other_module_builds_an_app_link(self):
        rx = r"matrix\.to/#/|/#/room/|[?&]surface=|surface=db"
        out = subprocess.run(["git", "grep", "-lE", rx, "--", "src", "scripts", "skills", "packages",
                              ":!*.md"], cwd=REPO, capture_output=True, text=True).stdout.split()
        self.assertEqual([p for p in out if p != "src/ag2space_links.py"], [])


if __name__ == "__main__":
    unittest.main()
