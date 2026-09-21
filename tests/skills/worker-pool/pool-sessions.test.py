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
            self.assertEqual(pool_sessions.sessions(Path(d)), [])

    def test_a_live_worker_carries_an_exact_match_argv(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live", "label": "Iiya"}},
                                {"!room:ag2.space": W1}, {W1: [open_run(W1)]})
            (row,) = pool_sessions.sessions(ws)
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
            (row,) = pool_sessions.sessions(ws)
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
            (row,) = pool_sessions.sessions(ws)
            self.assertEqual(row["tmux_socket"], SOCK)

    def test_rooms_group_per_worker_and_a_label_defaults_to_the_id(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(
                Path(d),
                {W1: {"state": "live", "label": "Iiya"}, W2: {"state": "live"}},
                {"!a:ag2.space": W1, "!b:ag2.space": W2, "!c:ag2.space": W2},
                {W1: [open_run(W1)], W2: [open_run(W2)]})
            rows = {r["worker_id"]: r for r in pool_sessions.sessions(ws)}
            self.assertEqual(rows[W1]["bound_rooms"], ["!a:ag2.space"])
            self.assertEqual(rows[W2]["bound_rooms"], ["!b:ag2.space", "!c:ag2.space"])
            self.assertEqual(rows[W2]["label"], W2)

    def test_it_never_describes_the_core(self):
        """Core socket resolution is the desktop app's policy; a copy here would
        be a second implementation that drifts."""
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(Path(d), {W1: {"state": "live"}}, {}, {W1: [open_run(W1)]})
            names = [r["session_name"] for r in pool_sessions.sessions(ws)]
            self.assertNotIn("sutando-core", names)

    def test_room_filter_selects_only_the_bound_worker(self):
        with tempfile.TemporaryDirectory() as d:
            ws = workspace_with(
                Path(d), {W1: {"state": "live"}, W2: {"state": "live"}},
                {"!a:ag2.space": W1, "!b:ag2.space": W2},
                {W1: [open_run(W1)], W2: [open_run(W2)]})
            rows = pool_sessions.sessions(ws)
            picked = [r for r in rows if "!b:ag2.space" in r["bound_rooms"]]
            self.assertEqual([r["worker_id"] for r in picked], [W2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
