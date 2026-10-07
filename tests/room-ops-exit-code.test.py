"""`--strict` makes room_ops' exit code follow its own ok; the default does not.

Measured 2026-09-08: `room_ops send <room> "<a message body>"` (send takes a PATH)
printed {"ok": false, "reason": "file not found"} and exited 0, so a shell caller
gating on the exit code read a failed send as delivered. It cost a real post.

The DEFAULT stays 0 — skills/agent-room-ops/test_room_ops.py pins that across
read/send/rooms/events (8 assertions), and notify_reviewers.py reads `ok`
explicitly rather than the exit code. --strict is for shell callers only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock

SRC = (pathlib.Path(__file__).resolve().parents[1] /
       "skills" / "agent-room-ops" / "room_ops.py")


def _load():
    s = importlib.util.spec_from_file_location("ro", SRC)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


class StrictIsOptIn(unittest.TestCase):
    """Behavioural only. keweichen mutated the predicate from `is False` to
    `is not None` and all six replica-based tests still passed."""

    def setUp(self):
        self.m = _load()

    def _rc(self, res, strict):
        return self.m._strict_rc(res, strict)

    def test_a_false_ok_exits_1_only_under_strict(self):
        bad = {"ok": False, "reason": "file not found"}
        self.assertEqual(self._rc(bad, True), 1)
        self.assertEqual(self._rc(bad, False), 0, "the default contract must not change")

    def test_a_TRUE_ok_exits_0_even_under_strict(self):
        """The mutation that survived: `is not None` would return 1 here."""
        self.assertEqual(self._rc({"ok": True, "event_id": "$x"}, True), 0)

    def test_a_result_without_an_ok_key_exits_0_even_under_strict(self):
        """No `ok` key: `is not None` leaves this at 0, so the ok:true case
        above is the one that kills that mutation. Pinned for the contract."""
        self.assertEqual(self._rc({"rooms": []}, True), 0)
        self.assertEqual(self._rc(None, True), 0)


class MainActuallyReturnsTheCode(unittest.TestCase):
    """Execute the real _main. The assertions above read source text and an
    exec'd replica, so line 292 (the --strict return) never ran under them."""

    def setUp(self):
        self.m = _load()

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.m._main(argv)
        return rc, buf.getvalue()

    def test_strict_returns_1_on_a_failed_op(self):
        rc, out = self._run(["--strict", "send", "!r:x", "/nope/missing.png"])
        self.assertEqual(rc, 1)
        self.assertIs(json.loads(out)["ok"], False)

    def test_the_default_returns_0_on_the_same_failed_op(self):
        rc, out = self._run(["send", "!r:x", "/nope/missing.png"])
        self.assertEqual(rc, 0, "the default contract must not change")
        self.assertIs(json.loads(out)["ok"], False)


    def test_events_stream_honours_strict_through_real_main(self):
        """keweichen: the stream branch returned before the strict mapping, so
        `--strict events stream --once` reported ok:false with rc 0.

        Stubbed, never dialled: calling it live BLOCKS on a real connection —
        I hung a terminal proving that before writing it this way.
        """
        buf = io.StringIO()
        with mock.patch.object(self.m._events, "stream",
                               side_effect=self.m._events.StreamDisconnected("no gateway configured")):
            with contextlib.redirect_stdout(buf):
                rc = self.m._main(["--strict", "events", "stream", "--once"])
        out = json.loads([l for l in buf.getvalue().strip().split("\n") if l.strip()][-1])
        self.assertIs(out["ok"], False, "the stub must produce the failure shape")
        self.assertEqual(rc, 1, "a failed stream must not exit 0 under --strict")

    def test_events_stream_still_exits_0_on_that_failure_WITHOUT_strict(self):
        buf = io.StringIO()
        with mock.patch.object(self.m._events, "stream",
                               side_effect=self.m._events.StreamDisconnected("no gateway configured")):
            with contextlib.redirect_stdout(buf):
                rc = self.m._main(["events", "stream", "--once"])
        self.assertEqual(rc, 0, "the default contract must not change")


if __name__ == "__main__":
    unittest.main(verbosity=1)
