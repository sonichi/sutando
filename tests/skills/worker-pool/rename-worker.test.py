#!/usr/bin/env python3
"""rename_worker changes a worker's label everywhere it is read, and nothing else.

The defect it guards: a label was settable only at creation, so a worker made
without one showed its 32-hex id in the desktop terminal tab and the picker for
life. A rename that reached only one reader would make the tab, the picker and
the router disagree about the worker's name.

Drives the real CLIs by subprocess, so each case fails on its own where the
rename command does not exist.

Run: python3 tests/skills/worker-pool/rename-worker.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))

import pool_advertise as pa  # noqa: E402
import pool_roster as pr  # noqa: E402

import worker_identity as wi  # noqa: E402

W1 = "02e4302f00844397bac09533fc398248"
W2 = "212e8040d38d48b5aadab0db295dc33a"


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          capture_output=True, text=True)


def make_pool(ws: Path) -> None:
    """Two workers through the production writers: identity records with a
    tmux socket under the temp dir, then roster registration."""
    for wid, label in ((W1, ""), (W2, "reviewer")):
        wi.create_worker(ws, runtime="claude", cwd=str(ws), worker_id=wid,
                         tmux_socket=str(ws / "tmux.sock"))
        pr.register_worker(ws, wid, label)


def identity_files(ws: Path, wid: str) -> dict:
    d = ws / "state" / "workers" / wid
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.json"))}


def sessions_row(ws: Path, wid: str) -> dict:
    r = run("pool_sessions.py", "list", "--workspace", str(ws))
    assert r.returncode == 0, r.stderr
    return next(s for s in json.loads(r.stdout)["sessions"] if s["worker_id"] == wid)


class RenameWorker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        make_pool(self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def rename(self, worker: str, label: str) -> subprocess.CompletedProcess:
        return run("rename_worker.py", "--worker", worker, "--label", label,
                   "--workspace", str(self.ws))

    def test_the_terminal_tab_list_shows_the_new_name_and_the_same_session(self):
        before = sessions_row(self.ws, W1)
        self.assertEqual(before["label"], W1)
        ids_before = identity_files(self.ws, W1)

        r = self.rename(W1, "Iiya")
        self.assertEqual(r.returncode, 0, r.stderr)

        after = sessions_row(self.ws, W1)
        self.assertEqual(after["label"], "Iiya")
        self.assertEqual(after["session_name"], f"sutando-worker-{W1}")
        self.assertEqual(after["session_name"], before["session_name"])
        self.assertEqual(after["tmux_socket"], before["tmux_socket"])
        # Identity records carry no label; a rename must not rewrite them.
        self.assertEqual(identity_files(self.ws, W1), ids_before)

    def test_roster_advertisement_and_router_agree_after_a_rename(self):
        v0 = pr.load_roster(self.ws)["version"]
        r = self.rename("reviewer", "code reviewer")
        self.assertEqual(r.returncode, 0, r.stderr)

        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["version"], v0 + 1)
        self.assertEqual(roster["workers"][W2]["label"], "code reviewer")
        self.assertEqual(roster["workers"][W2]["state"], "live")
        self.assertEqual(pr.resolve_label(roster, "code reviewer"), W2)
        self.assertEqual(pr.resolve_label(roster, "reviewer"), "reviewer")

        ad = json.loads(pa.advertisement_path(self.ws).read_text())
        self.assertEqual(ad["report"]["roster_version"], v0 + 1)
        self.assertEqual(ad["profile_workers"][W2]["label"], "code reviewer")
        self.assertEqual(ad["report"]["applied"]["labels"][W2], "code reviewer")
        self.assertEqual(sessions_row(self.ws, W2)["label"], "code reviewer")

    def test_a_rename_keeps_room_bindings(self):
        pr.bind_room(self.ws, "!room:ag2.space", W1)
        r = self.rename(W1, "Iiya")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pr.load_roster(self.ws)["bindings"], {"!room:ag2.space": W1})
        self.assertEqual(sessions_row(self.ws, W1)["bound_rooms"], ["!room:ag2.space"])

    def test_renaming_to_the_current_label_is_a_noop(self):
        v0 = pr.load_roster(self.ws)["version"]
        r = self.rename(W2, "reviewer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing changed", r.stdout)
        self.assertEqual(pr.load_roster(self.ws)["version"], v0)

    def assertRefused(self, r, why: str):
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("rename-worker:", r.stderr)
        self.assertIn(why, r.stderr)

    def test_refusals_change_nothing(self):
        roster0 = pr.roster_path(self.ws).read_bytes()
        ad0 = pa.advertisement_path(self.ws).read_bytes()
        cases = [
            (self.rename(W1, "   "), "cannot be empty"),
            (self.rename("nobody", "x"), "is not a worker id or label"),
            (self.rename(W1, "reviewer"), f"already names worker {W2}"),
            (self.rename(W1, W2), f"already names worker {W2}"),
            (self.rename(W1, "core"), "names the core"),
        ]
        for r, why in cases:
            with self.subTest(why=why):
                self.assertRefused(r, why)
        self.assertEqual(pr.roster_path(self.ws).read_bytes(), roster0)
        self.assertEqual(pa.advertisement_path(self.ws).read_bytes(), ad0)

    def test_a_label_two_workers_share_is_refused_as_the_target(self):
        pr.register_worker(self.ws, W1, "twin")
        pr.register_worker(self.ws, W2, "twin")
        roster0 = pr.roster_path(self.ws).read_bytes()
        self.assertRefused(self.rename("twin", "solo"), "is the label of 2 workers")
        self.assertEqual(pr.roster_path(self.ws).read_bytes(), roster0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
