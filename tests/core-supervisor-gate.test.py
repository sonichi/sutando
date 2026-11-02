#!/usr/bin/env python3
"""Tests for src/core-supervisor-gate.py — the notification-only death gate
(#2401 owner scope; john-the-dev #2404 review resolutions).

Covers the pure compound gate over the full tri-state signal matrix
(session_gone ∈ {True, False, None}), probe-UNKNOWN-holds (a tmux error is
never confirmed death), the sustained-streak rule, operator-intent
precedence, --sustain positivity validation, and the CORE-DEAD report path
(exit 3, nothing executed — the module has no relaunch capability at all).

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
    def test_full_tristate_matrix(self):
        # session_gone is tri-state; ONLY (stale, True, no-intent) trips.
        for hb in (False, True):
            for gone in (False, True, None):
                for intent in (False, True):
                    expect = hb and gone is True and not intent
                    self.assertEqual(_mod.evaluate(hb, gone, intent), expect,
                                     f"matrix case {(hb, gone, intent)}")

    def test_probe_unknown_never_confirms_death(self):
        # john-the-dev #2404 blocker (1): unknown probe ≠ dead.
        self.assertFalse(_mod.evaluate(True, None, False))

    def test_wedged_but_alive_never_trips(self):
        self.assertFalse(_mod.evaluate(False, True, False))

    def test_operator_intent_blocks_even_when_dead(self):
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
    module level — no real tmux server in tests (the #1428 flake class)."""

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
        self.assertIs(self._with_run(lambda *a, **k: self._R(0), "s.sock", "core"), False)

    def test_unrecognised_nonzero_is_unknown_not_dead(self):
        # rc 1 with no recognised absence message observed nothing: the gate holds.
        self.assertIsNone(self._with_run(lambda *a, **k: self._R(1), "s.sock", "core"))

    def test_signalled_client_is_unknown_not_dead(self):
        self.assertIsNone(self._with_run(lambda *a, **k: self._R(-9), "s.sock", "core"))

    def test_tmux_timeout_is_unknown_not_dead(self):
        import subprocess as sp

        def boom(*a, **k):
            raise sp.TimeoutExpired(cmd="tmux", timeout=10)
        self.assertIsNone(self._with_run(boom, "s.sock", "core"))

    def test_tmux_missing_is_unknown_not_dead(self):
        def boom(*a, **k):
            raise OSError("no tmux binary")
        self.assertIsNone(self._with_run(boom, "s.sock", "core"))

    def test_refused_client_is_unknown_not_dead(self):
        # A tmux client of another version than the server exits 1 with this
        # stderr before any session lookup — a dead PROBE, not a dead core.
        class _Skew(self._R):
            stderr = b"server exited unexpectedly\n"
        self.assertIsNone(self._with_run(lambda *a, **k: _Skew(1), "s.sock", "core"))

    def test_absent_with_miss_stderr_is_dead(self):
        class _Miss(self._R):
            stderr = b"can't find session: core\n"
        self.assertIs(self._with_run(lambda *a, **k: _Miss(1), "s.sock", "core"), True)


class TestOperatorIntent(unittest.TestCase):
    def test_absent_and_present(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "restart.sentinel")
            self.assertFalse(_mod.operator_intent(p))
            open(p, "w").close()
            self.assertTrue(_mod.operator_intent(p))

    def test_empty_path_means_no_intent(self):
        self.assertFalse(_mod.operator_intent(""))


class TestSustainValidation(unittest.TestCase):
    def test_sustain_zero_rejected(self):
        # john-the-dev #2404 blocker (3): --sustain 0 must be refused, not
        # silently fire on the first tick.
        with self.assertRaises(SystemExit):
            _mod.main(["tick", "--alive", "/nonexistent", "--socket", "s",
                       "--state-file", "/tmp/x", "--sustain", "0"])

    def test_stale_sec_zero_and_negative_rejected(self):
        # qingyun #2404 P1 round 2: a non-positive --stale-sec classifies a
        # FRESH heartbeat as stale (now - mtime > 0 always), letting a
        # confirmed-absent session report CORE-DEAD immediately. Refuse it.
        for bad in ("0", "-5", "-0.1"):
            with self.assertRaises(SystemExit):
                _mod.main(["tick", "--alive", "/nonexistent", "--socket", "s",
                           "--state-file", "/tmp/x", "--stale-sec", bad])

    def test_sustain_negative_rejected(self):
        with self.assertRaises(SystemExit):
            _mod.main(["tick", "--alive", "/nonexistent", "--socket", "s",
                       "--state-file", "/tmp/x", "--sustain", "-3"])


def _tick(td, alive_age, gone, sentinel=False, sustain=2):
    """One CLI tick against fixture signals; returns (exit_code, streak).
    ``gone`` is the tri-state session_gone result to inject."""
    alive = os.path.join(td, "h.alive")
    with open(alive, "w") as f:
        f.write("{}")
    os.utime(alive, (time.time() - alive_age, time.time() - alive_age))
    sent = os.path.join(td, "restart.sentinel")
    if sentinel:
        open(sent, "w").close()
    orig = _mod.session_gone
    _mod.session_gone = lambda socket, session: gone
    try:
        rc = _mod.main(["tick", "--alive", alive,
                        "--socket", os.path.join(td, "no.sock"),
                        "--restart-sentinel", sent,
                        "--state-file", os.path.join(td, "gate.state"),
                        "--sustain", str(sustain)])
    finally:
        _mod.session_gone = orig
    with open(os.path.join(td, "gate.state")) as f:
        st = json.load(f)
    return rc, st["streak"]


def _reported(td):
    with open(os.path.join(td, "gate.state")) as f:
        return json.load(f).get("reported", False)


class TestSustainedStreakCli(unittest.TestCase):
    def test_outage_signals_need_two_ticks_then_report(self):
        # 2026-07-29 reproduction: stale heartbeat + confirmed-absent session.
        with tempfile.TemporaryDirectory() as td:
            rc1, s1 = _tick(td, alive_age=600, gone=True)
            self.assertEqual((rc1, s1), (0, 1))  # first tick holds
            rc2, s2 = _tick(td, alive_age=600, gone=True)
            self.assertEqual(rc2, 3)             # sustained → CORE-DEAD report
            self.assertTrue(_reported(td))       # latch set — same outage won't re-notify

    def test_probe_unknown_holds_at_cli_level(self):
        # Blocker (1) end-to-end: stale heartbeat + UNKNOWN probe → hold forever.
        with tempfile.TemporaryDirectory() as td:
            for _ in range(3):
                rc, s = _tick(td, alive_age=600, gone=None)
                self.assertEqual((rc, s), (0, 0))

    def test_healthy_tick_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            _tick(td, alive_age=600, gone=True)
            rc, s = _tick(td, alive_age=0, gone=False)  # core back
            self.assertEqual((rc, s), (0, 0))
            rc3, s3 = _tick(td, alive_age=600, gone=True)
            self.assertEqual((rc3, s3), (0, 1))  # must re-earn both ticks

    def test_live_session_holds_even_with_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            for _ in range(3):
                rc, s = _tick(td, alive_age=600, gone=False)
                self.assertEqual((rc, s), (0, 0))

    def test_sentinel_blocks_sustained_death(self):
        with tempfile.TemporaryDirectory() as td:
            for _ in range(3):
                rc, s = _tick(td, alive_age=600, gone=True, sentinel=True)
                self.assertEqual((rc, s), (0, 0))

    def test_persistent_outage_reports_exactly_once(self):
        # qingyun + john-the-dev #2404 (2026-07-30): without a reported latch,
        # a persistent outage re-earned the threshold and exited 3 every
        # --sustain ticks (their 4-tick repro: rc 0,3,0,3). One outage must
        # notify exactly once.
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_tick(td, alive_age=600, gone=True)[0], 0)   # earn
            self.assertEqual(_tick(td, alive_age=600, gone=True)[0], 3)   # report
            for _ in range(4):                                            # same outage continues
                rc, _s = _tick(td, alive_age=600, gone=True)
                self.assertEqual(rc, 0)                                   # silent — no storm
                self.assertTrue(_reported(td))

    def test_recovery_clears_latch_and_new_death_reports_again(self):
        # sustained death → one report → healthy reset → NEW sustained death
        # must report once again (the full cycle both reviewers asked pinned).
        with tempfile.TemporaryDirectory() as td:
            _tick(td, alive_age=600, gone=True)
            self.assertEqual(_tick(td, alive_age=600, gone=True)[0], 3)   # outage 1 reported
            rc, s = _tick(td, alive_age=0, gone=False)                    # core back
            self.assertEqual((rc, s), (0, 0))
            self.assertFalse(_reported(td))                               # latch cleared
            self.assertEqual(_tick(td, alive_age=600, gone=True), (0, 1))  # re-earn
            self.assertEqual(_tick(td, alive_age=600, gone=True)[0], 3)   # outage 2 reports once

    def test_probe_unknown_hold_clears_latch(self):
        # Reviewers: "until a healthy or HOLDING observation clears it" — an
        # UNKNOWN-probe hold also ends the reported outage window.
        with tempfile.TemporaryDirectory() as td:
            _tick(td, alive_age=600, gone=True)
            self.assertEqual(_tick(td, alive_age=600, gone=True)[0], 3)
            self.assertEqual(_tick(td, alive_age=600, gone=None)[0], 0)   # holding tick
            self.assertFalse(_reported(td))

    def test_report_executes_nothing(self):
        # The module must have NO execution capability at all: a sustained
        # trip spawns no process (Popen absent from the module namespace).
        self.assertFalse(hasattr(_mod, "Popen"))
        src = open(_SRC).read()
        self.assertNotIn("Popen", src)
        self.assertNotIn("relaunch_cmd", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
