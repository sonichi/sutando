#!/usr/bin/env python3
"""pool_sessions reports the worker tmux sessions a viewer could attach.

The defect it guards: a viewer that cannot NAME a worker's session can only ever
show the core, and a lister that drops a worker between incarnations is
indistinguishable from a pool with no workers at all.

Run: python3 tests/skills/worker-pool/pool-sessions.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_sessions  # noqa: E402

def EXISTS(name, socket=None):
    return ("exists", "")


def ABSENT(name, socket=None):
    return ("absent", "no server")


def UNKNOWN(name, socket=None):
    return ("unknown", "tmux could not be run")

W1 = "02e4302f00844397bac09533fc398248"
W2 = "212e8040d38d48b5aadab0db295dc33a"
SOCK = "/run/tmux.sock"


def write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def workspace_with(tmp: Path, workers: dict, bindings: dict, incarnations: dict) -> Path:
    write(tmp / "state" / "roster.json",
          {"version": 1, "workers": workers, "bindings": bindings})
    for wid, rows in incarnations.items():
        write(tmp / "state" / "workers" / wid / "incarnations.json", {"incarnations": rows})
    return tmp


def open_run(wid: str, socket: str = SOCK) -> dict:
    return {"incarnation_id": "i-" + wid[:6], "session_id": "s1", "ended_at": None,
            "tmux": {"socket": socket, "session_name": f"sutando-worker-{wid}"}}


def closed_run(wid: str) -> dict:
    return {"incarnation_id": "old-" + wid[:6], "session_id": "s0", "ended_at": "2026-09-20T00:00:00Z",
            "tmux": {"socket": SOCK, "session_name": f"sutando-worker-{wid}"}}


class PoolSessions(unittest.TestCase):
    def test_no_roster_is_an_empty_list(self):
        """The isolation case: an install with no pool yields nothing to attach,
        so a caller never has a session name to send."""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pool_sessions.sessions(Path(d), probe=EXISTS), [])

    def test_a_live_worker_carries_an_exact_match_argv(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live", "label": "Iiya"}},
                                {"!room:ag2.space": W1}, {W1: [open_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=EXISTS)
            self.assertEqual(row["label"], "Iiya")
            self.assertTrue(row["live"])
            self.assertEqual(row["bound_rooms"], ["!room:ag2.space"])
            self.assertEqual(row["session_name"], f"sutando-worker-{W1}")
            # `=` or tmux prefix-matches and a short id lands on another worker.
            self.assertEqual(row["attach_argv"][-1], f"=sutando-worker-{W1}")
            self.assertEqual(row["attach_argv"][:3], ["tmux", "-S", SOCK])

    def test_a_worker_between_incarnations_is_reported_not_dropped(self):
        """`live: false` and no argv — "exists, no session now" must stay
        distinguishable from "no such worker"."""
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live", "label": "Iiya"}},
                                {}, {W1: [closed_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=EXISTS)
            self.assertFalse(row["live"])
            self.assertIsNone(row["attach_argv"])
            self.assertEqual(row["session_name"], f"sutando-worker-{W1}")

    def test_the_newest_open_incarnation_wins(self):
        """A crash can leave an older run unclosed; the latest open row is the
        one a viewer should attach."""
        with tempfile.TemporaryDirectory() as d:
            stale = open_run(W1, socket="/stale.sock")
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {},
                                {W1: [stale, open_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=EXISTS)
            self.assertEqual(row["tmux_socket"], SOCK)

    def test_rooms_group_per_worker_and_a_label_defaults_to_the_id(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(
                Path(d),
                {W1: {"state": "live", "label": "Iiya"}, W2: {"state": "live"}},
                {"!a:ag2.space": W1, "!b:ag2.space": W2, "!c:ag2.space": W2},
                {W1: [open_run(W1)], W2: [open_run(W2)]})
            rows = {r["worker_id"]: r for r in pool_sessions.sessions(ws, probe=EXISTS)}
            self.assertEqual(rows[W1]["bound_rooms"], ["!a:ag2.space"])
            self.assertEqual(rows[W2]["bound_rooms"], ["!b:ag2.space", "!c:ag2.space"])
            self.assertEqual(rows[W2]["label"], W2)

    def test_it_never_describes_the_core(self):
        """Core socket resolution is the desktop app's policy; a copy here would
        be a second implementation that drifts."""
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {}, {W1: [open_run(W1)]})
            names = [r["session_name"] for r in pool_sessions.sessions(ws, probe=EXISTS)]
            self.assertNotIn("sutando-core", names)

    def test_room_filter_selects_only_the_bound_worker(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(
                Path(d), {W1: {"state": "live"}, W2: {"state": "live"}},
                {"!a:ag2.space": W1, "!b:ag2.space": W2},
                {W1: [open_run(W1)], W2: [open_run(W2)]})
            rows = pool_sessions.sessions(ws, probe=EXISTS)
            picked = [r for r in rows if "!b:ag2.space" in r["bound_rooms"]]
            self.assertEqual([r["worker_id"] for r in picked], [W2])


class LivenessIsProbedNotInferred(unittest.TestCase):
    """An unclosed incarnation proves a run was STARTED there, never that the
    session is still up — a crash is explicitly allowed to leave the row open."""

    def test_a_stale_unclosed_record_is_not_live(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {}, {W1: [open_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=ABSENT)
            self.assertEqual(row["availability"], "absent")
            self.assertFalse(row["live"])
            self.assertIsNone(row["attach_argv"])

    def test_the_stale_worker_is_still_reported_not_dropped(self):
        """Unavailable must stay visible: dropping it is indistinguishable from
        the worker not existing."""
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live", "label": "Iiya"}}, {}, {W1: [open_run(W1)]})
            rows = pool_sessions.sessions(ws, probe=ABSENT)
            self.assertEqual([r["worker_id"] for r in rows], [W1])
            self.assertEqual(rows[0]["label"], "Iiya")

    def test_a_probe_that_cannot_answer_is_not_live(self):
        """Three states, not two. `unknown` must not read as available — an argv
        that cannot attach is worse than none, because the caller acts on it."""
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {}, {W1: [open_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=UNKNOWN)
            self.assertEqual(row["availability"], "unknown")
            self.assertFalse(row["live"])
            self.assertIsNone(row["attach_argv"])

    def test_an_older_unclosed_run_does_not_resurrect_a_closed_worker(self):
        """Newer run started AND closed, older one left unclosed: the probe, not
        the record, decides."""
        with tempfile.TemporaryDirectory() as d:
            stale = open_run(W1)
            stale["incarnation_id"] = "older-unclosed"
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {},
                                {W1: [stale, closed_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=ABSENT)
            self.assertFalse(row["live"])
            self.assertIsNone(row["attach_argv"])


class BindingsUseTheRostersOwnSemantics(unittest.TestCase):
    """`pool_roster.targets_for` accepts a bare id OR a set; a direct `==`
    silently drops the set form."""

    def test_a_list_binding_is_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}},
                                {"!room:ag2.space": [W1]}, {W1: [open_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=EXISTS)
            self.assertEqual(row["bound_rooms"], ["!room:ag2.space"])

    def test_a_bare_string_binding_still_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}},
                                {"!room:ag2.space": W1}, {W1: [open_run(W1)]})
            (row,) = pool_sessions.sessions(ws, probe=EXISTS)
            self.assertEqual(row["bound_rooms"], ["!room:ag2.space"])

    def test_a_multi_member_set_binding_resolves_for_each_member(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d),
                                {W1: {"state": "live"}, W2: {"state": "live"}},
                                {"!room:ag2.space": [W1, W2]},
                                {W1: [open_run(W1)], W2: [open_run(W2)]})
            rows = {r["worker_id"]: r for r in pool_sessions.sessions(ws, probe=EXISTS)}
            self.assertEqual(rows[W1]["bound_rooms"], ["!room:ag2.space"])
            self.assertEqual(rows[W2]["bound_rooms"], ["!room:ag2.space"])


class ThroughTheActualCLI(unittest.TestCase):
    """The reviewer asked for both binding forms driven through the CLI, not
    just the function — the CLI is what the desktop app invokes."""

    def run_cli(self, ws, *args):
        import subprocess
        script = REPO / "skills/worker-pool/scripts/pool_sessions.py"
        r = subprocess.run([sys.executable, str(script), "list", "--workspace", str(ws), *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["sessions"]

    def test_room_filter_finds_a_list_binding(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}},
                                {"!room:ag2.space": [W1]}, {W1: [open_run(W1)]})
            rows = self.run_cli(ws, "--room", "!room:ag2.space")
            self.assertEqual([r["worker_id"] for r in rows], [W1])

    def test_room_filter_finds_a_bare_string_binding(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}},
                                {"!room:ag2.space": W1}, {W1: [open_run(W1)]})
            rows = self.run_cli(ws, "--room", "!room:ag2.space")
            self.assertEqual([r["worker_id"] for r in rows], [W1])

    def test_the_cli_does_not_advertise_a_session_that_is_not_there(self):
        """End to end with the REAL probe: the recorded socket does not exist."""
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {},
                                {W1: [open_run(W1, socket=str(Path(d) / "no-such.sock"))]})
            (row,) = self.run_cli(ws)
            self.assertFalse(row["live"])
            self.assertIsNone(row["attach_argv"])
            self.assertIn(row["availability"], ("absent", "unknown"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
