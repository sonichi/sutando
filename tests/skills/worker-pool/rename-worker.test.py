#!/usr/bin/env python3
"""rename_worker changes a worker's label everywhere it is read, and nothing else.

The defect it guards: a label was settable only at creation, so a worker made
without one showed its 32-hex id in the desktop terminal tab and the picker for
life. A rename that reached only one reader would make the tab, the picker and
the router disagree about the worker's name.

The writer and the CLI are called in-process, so the coverage runner measures
them; one subprocess case proves the installed command end to end.

Run: python3 tests/skills/worker-pool/rename-worker.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))

import pool_advertise as pa  # noqa: E402
import pool_roster as pr  # noqa: E402

import pool_sessions  # noqa: E402
import rename_worker as rw  # noqa: E402

import worker_identity as wi  # noqa: E402

W1 = "02e4302f00844397bac09533fc398248"
W2 = "212e8040d38d48b5aadab0db295dc33a"


def ABSENT(name, socket=None):
    return ("absent", "no server")


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


def session_row(ws: Path, wid: str) -> dict:
    return next(r for r in pool_sessions.sessions(ws, probe=ABSENT) if r["worker_id"] == wid)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        make_pool(self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args: str) -> "tuple[int, str, str]":
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rw.main([*args, "--workspace", str(self.ws)])
        return rc, out.getvalue(), err.getvalue()

    def frozen(self) -> "tuple[bytes, bytes]":
        return pr.roster_path(self.ws).read_bytes(), pa.advertisement_path(self.ws).read_bytes()


class Writer(Base):
    """pool_roster.rename_worker, the one writer."""

    def test_a_rename_reaches_the_tab_list_and_keeps_the_session(self):
        before = session_row(self.ws, W1)
        self.assertEqual(before["label"], W1)
        ids_before = identity_files(self.ws, W1)

        wid, old, roster = pr.rename_worker(self.ws, W1, "  Iiya ")
        self.assertEqual((wid, old), (W1, W1))
        self.assertEqual(roster["workers"][W1]["label"], "Iiya")

        after = session_row(self.ws, W1)
        self.assertEqual(after["label"], "Iiya")
        self.assertEqual(after["session_name"], f"sutando-worker-{W1}")
        self.assertEqual(after["session_name"], before["session_name"])
        self.assertEqual(after["tmux_socket"], before["tmux_socket"])
        # Identity records carry no label; a rename must not rewrite them.
        self.assertEqual(identity_files(self.ws, W1), ids_before)

    def test_roster_advertisement_and_router_agree_after_a_rename_by_label(self):
        v0 = pr.load_roster(self.ws)["version"]
        wid, old, _ = pr.rename_worker(self.ws, "reviewer", "code reviewer")
        self.assertEqual((wid, old), (W2, "reviewer"))

        roster = pr.load_roster(self.ws)
        self.assertEqual(roster["version"], v0 + 1)
        self.assertEqual(roster["workers"][W2], {"state": "live", "label": "code reviewer"})
        self.assertEqual(pr.resolve_label(roster, "code reviewer"), W2)
        self.assertEqual(pr.resolve_label(roster, "reviewer"), "reviewer")

        ad = json.loads(pa.advertisement_path(self.ws).read_text())
        self.assertEqual(ad["report"]["roster_version"], v0 + 1)
        self.assertEqual(ad["profile_workers"][W2]["label"], "code reviewer")
        self.assertEqual(ad["report"]["applied"]["labels"][W2], "code reviewer")

    def test_a_rename_keeps_room_bindings(self):
        pr.bind_room(self.ws, "!room:ag2.space", W1)
        pr.rename_worker(self.ws, W1, "Iiya")
        self.assertEqual(pr.load_roster(self.ws)["bindings"], {"!room:ag2.space": W1})
        self.assertEqual(session_row(self.ws, W1)["bound_rooms"], ["!room:ag2.space"])

    def test_renaming_to_the_current_label_is_a_noop(self):
        before = self.frozen()
        self.assertEqual(pr.rename_worker(self.ws, W2, "reviewer"), (W2, "reviewer", None))
        self.assertEqual(self.frozen(), before)

    def test_refusals_change_nothing(self):
        before = self.frozen()
        cases = [
            (W1, "   ", "cannot be empty"),
            (W1, None, "cannot be empty"),
            ("nobody", "x", "is not a worker id or label"),
            (W1, "reviewer", f"already names worker {W2}"),
            (W1, W2, f"already names worker {W2}"),
            (W1, "core", "names the core"),
        ]
        for target, label, why in cases:
            with self.subTest(why=why, label=label):
                with self.assertRaisesRegex(pr.RosterError, why):
                    pr.rename_worker(self.ws, target, label)
        self.assertEqual(self.frozen(), before)

    def test_a_label_two_workers_share_is_refused_as_the_target(self):
        pr.register_worker(self.ws, W1, "twin")
        pr.register_worker(self.ws, W2, "twin")
        before = self.frozen()
        with self.assertRaisesRegex(pr.RosterError, "is the label of 2 workers"):
            pr.rename_worker(self.ws, "twin", "solo")
        self.assertEqual(self.frozen(), before)

    def test_no_roster_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(pr.RosterError, "no roster"):
                pr.rename_worker(d, W1, "x")


class Cli(Base):
    """rename_worker.main: exit codes and what it tells the caller."""

    def test_rename_prints_old_and_new_and_the_roster_version(self):
        rc, out, err = self.cli("--worker", W1, "--label", "Iiya")
        self.assertEqual(rc, 0, err)
        v = pr.load_roster(self.ws)["version"]
        self.assertEqual(out.strip(), f"worker {W1} renamed {W1!r} -> 'Iiya' (roster v{v})")

    def test_json_reports_changed_and_version(self):
        rc, out, _ = self.cli("--worker", "reviewer", "--label", "code reviewer", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc, {"worker_id": W2, "old_label": "reviewer",
                               "label": "code reviewer", "changed": True,
                               "roster_version": pr.load_roster(self.ws)["version"]})

    def test_noop_exits_zero_in_both_forms(self):
        rc, out, _ = self.cli("--worker", W2, "--label", "reviewer")
        self.assertEqual(rc, 0)
        self.assertIn("nothing changed", out)
        rc, out, _ = self.cli("--worker", W2, "--label", "reviewer", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["changed"], False)
        self.assertIsNone(json.loads(out)["roster_version"])

    def test_a_refusal_exits_2_and_names_the_reason(self):
        rc, out, err = self.cli("--worker", "nobody", "--label", "x")
        self.assertEqual((rc, out), (rw.REFUSED, ""))
        self.assertIn("rename-worker: 'nobody' is not a worker id or label", err)

    def test_an_unpublished_advertisement_exits_1_with_the_repair(self):
        with mock.patch.object(pa, "write_advertisement", side_effect=OSError("disk full")):
            rc, _, err = self.cli("--worker", W1, "--label", "Iiya")
        self.assertEqual(rc, 1)
        self.assertIn("the roster is renamed", err)
        self.assertIn("disk full", err)
        self.assertIn("pool_advertise.py --workspace", err)
        # The roster write happened; only the derived file lags.
        self.assertEqual(pr.load_roster(self.ws)["workers"][W1]["label"], "Iiya")

    def test_an_unwritable_roster_exits_1(self):
        with mock.patch.object(pr, "rename_worker", side_effect=PermissionError("read-only")):
            rc, _, err = self.cli("--worker", W1, "--label", "Iiya")
        self.assertEqual(rc, 1)
        self.assertIn("the roster could not be written: read-only", err)


class Smoke(Base):
    def test_the_installed_command_renames_what_pool_sessions_lists(self):
        run = lambda *a: subprocess.run([sys.executable, *map(str, a)],
                                        capture_output=True, text=True)
        r = run(SCRIPTS / "rename_worker.py", "--worker", W1, "--label", "Iiya",
                "--workspace", self.ws)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run(SCRIPTS / "pool_sessions.py", "list", "--workspace", self.ws)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = next(s for s in json.loads(r.stdout)["sessions"] if s["worker_id"] == W1)
        self.assertEqual((row["label"], row["session_name"]), ("Iiya", f"sutando-worker-{W1}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
