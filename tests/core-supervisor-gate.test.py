#!/usr/bin/env python3
"""Tests for src/core-supervisor-gate.py — the RECOVER decision gate (#2401).

Covers the pure compound gate (full 8-combination signal matrix), the
sustained-streak rule (a single tripped tick never acts; a healthy tick
resets), operator-intent precedence, heartbeat-staleness edges (missing
file = stale), and full dry-run CLI cycles reproducing the 2026-07-29
outage signals (stale .alive + no tmux session + no sentinel).

Run: python3 tests/core-supervisor-gate.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-supervisor-gate.py")
_spec = importlib.util.spec_from_file_location("core_supervisor_gate", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestEvaluateMatrix(unittest.TestCase):
    def test_full_matrix(self):
        # (hb_stale, session_gone, operator_intent) → only (True, True, False) trips.
        for hb in (False, True):
            for gone in (False, True):
                for intent in (False, True):
                    expect = hb and gone and not intent
                    self.assertEqual(_mod.evaluate(hb, gone, intent), expect,
                                     f"matrix case {(hb, gone, intent)}")

    def test_wedged_but_alive_never_trips(self):
        # 1428-class false positive: heartbeat fresh (core alive but quiet).
        self.assertFalse(_mod.evaluate(False, True, False))

    def test_operator_intent_blocks_even_when_dead(self):
        # Planned restart mid-flight: both death signals true, sentinel present.
        self.assertFalse(_mod.evaluate(True, True, True))


class TestHeartbeatStale(unittest.TestCase):
    def test_missing_file_is_stale(self):
        self.assertTrue(_mod.heartbeat_stale("/nonexistent/x.alive", 90))

    def test_fresh_and_stale_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "h.alive")
            with open(p, "w") as f:
                f.write("{}")
            now = time.time()
            self.assertFalse(_mod.heartbeat_stale(p, 90, now=now))
            self.assertTrue(_mod.heartbeat_stale(p, 90, now=now + 91))


class TestSessionGone(unittest.TestCase):
    """Exercise session_gone's real body with subprocess.run stubbed at the
    module level — spawning a real tmux server in tests is the flake class
    #1428 warns about, but the decision paths still need coverage."""

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def _with_run(self, fake, *call):
        import subprocess as sp
        orig = sp.run
        sp.run = fake
        try:
            return _mod.session_gone(*call)
        finally:
            sp.run = orig

    def test_session_present(self):
        self.assertFalse(self._with_run(lambda *a, **k: self._R(0), "s.sock", "core"))

    def test_session_absent(self):
        self.assertTrue(self._with_run(lambda *a, **k: self._R(1), "s.sock", "core"))

    def test_tmux_timeout_treated_as_gone(self):
        import subprocess as sp

        def boom(*a, **k):
            raise sp.TimeoutExpired(cmd="tmux", timeout=10)
        self.assertTrue(self._with_run(boom, "s.sock", "core"))

    def test_tmux_missing_treated_as_gone(self):
        def boom(*a, **k):
            raise OSError("no tmux binary")
        self.assertTrue(self._with_run(boom, "s.sock", "core"))


class TestOperatorIntent(unittest.TestCase):
    def test_absent_and_present(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "restart.sentinel")
            self.assertFalse(_mod.operator_intent(p))
            open(p, "w").close()
            self.assertTrue(_mod.operator_intent(p))

    def test_empty_path_means_no_intent(self):
        self.assertFalse(_mod.operator_intent(""))


def _tick(td, alive_age, session_exists, sentinel=False, sustain=2, dry=True):
    """Run one CLI tick against fixture signals; returns (exit_code, streak)."""
    alive = os.path.join(td, "h.alive")
    with open(alive, "w") as f:
        f.write("{}")
    os.utime(alive, (time.time() - alive_age, time.time() - alive_age))
    sent = os.path.join(td, "restart.sentinel")
    if sentinel:
        open(sent, "w").close()
    # A socket path nothing listens on → has-session fails → session gone.
    # For session_exists we point at a path we stub via monkeypatch instead —
    # spawning a real tmux server in tests is exactly the flake #1428 warns about.
    orig = _mod.session_gone
    _mod.session_gone = lambda socket, session: not session_exists
    try:
        rc = _mod.main(["tick", "--alive", alive,
                        "--socket", os.path.join(td, "no.sock"),
                        "--restart-sentinel", sent,
                        "--state-file", os.path.join(td, "gate.state"),
                        "--sustain", str(sustain)] + (["--dry-run"] if dry else []))
    finally:
        _mod.session_gone = orig
    with open(os.path.join(td, "gate.state")) as f:
        streak = json.load(f)["streak"]
    return rc, streak


class TestSustainedStreakCli(unittest.TestCase):
    def test_outage_signals_need_two_ticks(self):
        # 2026-07-29 reproduction: stale heartbeat + no session + no sentinel.
        with tempfile.TemporaryDirectory() as td:
            rc1, s1 = _tick(td, alive_age=600, session_exists=False)
            self.assertEqual((rc1, s1), (0, 1))  # first tick holds
            rc2, s2 = _tick(td, alive_age=600, session_exists=False)
            self.assertEqual(rc2, 3)             # sustained → WOULD-RELAUNCH
            self.assertEqual(s2, 0)              # acted once, re-armed

    def test_healthy_tick_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            _tick(td, alive_age=600, session_exists=False)
            rc, s = _tick(td, alive_age=0, session_exists=True)  # core back
            self.assertEqual((rc, s), (0, 0))
            rc3, s3 = _tick(td, alive_age=600, session_exists=False)
            self.assertEqual((rc3, s3), (0, 1))  # must re-earn both ticks

    def test_live_session_holds_even_with_stale_heartbeat(self):
        # Wedged-but-alive: session present → never trips regardless of streak.
        with tempfile.TemporaryDirectory() as td:
            for _ in range(3):
                rc, s = _tick(td, alive_age=600, session_exists=True)
                self.assertEqual((rc, s), (0, 0))

    def test_sentinel_blocks_sustained_death(self):
        with tempfile.TemporaryDirectory() as td:
            for _ in range(3):
                rc, s = _tick(td, alive_age=600, session_exists=False, sentinel=True)
                self.assertEqual((rc, s), (0, 0))

    def test_live_relaunch_executes_cmd(self):
        # Without --dry-run, a sustained trip must actually spawn the relaunch
        # command (detached). Marker file proves execution.
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "ran")
            alive = os.path.join(td, "h.alive")
            open(alive, "w").close()
            os.utime(alive, (time.time() - 600,) * 2)
            orig = _mod.session_gone
            _mod.session_gone = lambda socket, session: True
            try:
                args = ["tick", "--alive", alive, "--socket", "s", "--state-file",
                        os.path.join(td, "gate.state"), "--sustain", "1",
                        "--relaunch-cmd", f"touch {marker}"]
                self.assertEqual(_mod.main(args), 3)
            finally:
                _mod.session_gone = orig
            for _ in range(30):  # Popen is detached; poll briefly
                if os.path.exists(marker):
                    break
                time.sleep(0.1)
            self.assertTrue(os.path.exists(marker))

    def test_dry_run_never_executes(self):
        # relaunch-cmd absent + dry-run: exit 3 but nothing spawned (the cmd
        # would create a marker file if executed).
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "ran")
            alive = os.path.join(td, "h.alive")
            open(alive, "w").close()
            os.utime(alive, (time.time() - 600,) * 2)
            orig = _mod.session_gone
            _mod.session_gone = lambda socket, session: True
            try:
                args = ["tick", "--alive", alive, "--socket", "s", "--state-file",
                        os.path.join(td, "gate.state"), "--sustain", "1",
                        "--relaunch-cmd", f"touch {marker}", "--dry-run"]
                self.assertEqual(_mod.main(args), 3)
            finally:
                _mod.session_gone = orig
            self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main(verbosity=2)
