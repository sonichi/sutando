#!/usr/bin/env python3
"""Fail-closed branch tests for scripts/voice-lock.py."""
from __future__ import annotations

import builtins
import contextlib
import errno
import importlib.util
import io
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "..", "scripts", "voice-lock.py")
SPEC = importlib.util.spec_from_file_location("voice_lock", SOURCE)
voice_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice_lock)


def args(**overrides):
    values = {
        "guard": "/tmp/voice-lock.guard",
        "pidfile": "/tmp/voice-agent.pid",
        "pid": 101,
        "start_time_ms": 1_000,
        "pgid": 101,
        "entry": ["/tmp/voice-agent.py"],
        "workspace": "/tmp/workspace",
        "port": 9900,
        "term_wait_ms": 0,
        "kill_wait_ms": 0,
        "expect_pid": 101,
        "expect_start_time_ms": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def structured(pid=101, start_time_ms=1_000):
    return {
        "kind": "structured",
        "v": 1,
        "lockId": "vl1-test",
        "pid": pid,
        "startTimeMs": start_time_ms,
        "entry": "/tmp/voice-agent.py",
        "workspace": "/tmp/workspace",
    }


class VoiceLockBranchesTest(unittest.TestCase):
    def assert_emit(self, code, fn):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)
        return output.getvalue()

    def guard_patch(self):
        return mock.patch.object(voice_lock, "Guard", lambda _path: contextlib.nullcontext())

    def test_process_inspection_failures_are_conservative(self):
        with mock.patch.object(voice_lock.subprocess, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(voice_lock._run(["unused"]), "")
        with mock.patch.object(voice_lock.os, "kill", side_effect=PermissionError), \
                mock.patch.object(voice_lock, "_ps", return_value="S"):
            self.assertTrue(voice_lock.pid_alive(101))
        with mock.patch.object(voice_lock.os, "kill", side_effect=OSError):
            self.assertFalse(voice_lock.pid_alive(101))
        with mock.patch.object(voice_lock, "_ps", return_value=""):
            self.assertIsNone(voice_lock.pid_start_time_ms(101))
        with mock.patch.object(voice_lock, "_ps", return_value="not-a-date"):
            self.assertIsNone(voice_lock.pid_start_time_ms(101))
        with mock.patch.object(voice_lock.os, "getpgid", side_effect=OSError), \
                mock.patch.object(voice_lock, "_ps", return_value=" 42 "):
            self.assertEqual(voice_lock.pid_pgid(101), 42)
        with mock.patch.object(voice_lock.os, "getpgid", side_effect=OSError), \
                mock.patch.object(voice_lock, "_ps", return_value="invalid"):
            self.assertIsNone(voice_lock.pid_pgid(101))

    def test_process_listing_parsers_ignore_bad_rows(self):
        listing = "1 55 S\nnot-a-pid 55 S\n2 invalid S\n3 55 Z\n"
        with mock.patch.object(voice_lock, "_ps", return_value=listing):
            self.assertEqual(voice_lock.pgid_member_pids(55), [1])
        with mock.patch.object(voice_lock, "_run", return_value="12\ninvalid\n"):
            self.assertEqual(voice_lock.listener_pids(9900), [12])
        self.assertFalse(voice_lock.start_times_match(None, 1_000))
        with mock.patch.object(voice_lock.os.path, "realpath", side_effect=OSError):
            self.assertEqual(voice_lock._realpath("relative"), "relative")

    def test_lock_read_edge_cases(self):
        with mock.patch.object(builtins, "open", side_effect=PermissionError):
            self.assertEqual(voice_lock.read_lock("unused"), {"kind": "unknown"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            with open(path, "w") as handle:
                handle.write("\n")
            self.assertEqual(voice_lock.read_lock(path), {"kind": "unknown"})
            with open(path, "w") as handle:
                handle.write("[]")
            self.assertEqual(voice_lock.read_lock(path), {"kind": "unknown"})

    def test_liveness_and_lock_creation_edge_cases(self):
        lock = structured(start_time_ms=1_000)
        with mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                mock.patch.object(voice_lock, "pid_start_time_ms", return_value=9_000):
            self.assertEqual(voice_lock._owner_liveness(lock), "stale")
        with mock.patch.object(voice_lock, "pid_start_time_ms", return_value=None):
            self.assert_emit(
                4,
                lambda: voice_lock._create_lock("unused", 101, "/tmp/entry", "/tmp/ws"),
            )

    def test_acquire_handles_unlink_race_and_exclusive_create_race(self):
        held = structured()
        create_error = FileExistsError(errno.EEXIST, "exists")
        with self.guard_patch(), \
                mock.patch.object(voice_lock, "read_lock", side_effect=[{"kind": "legacy", "pid": 999}, held]), \
                mock.patch.object(voice_lock, "_owner_liveness", return_value="stale"), \
                mock.patch.object(voice_lock.os, "unlink", side_effect=FileNotFoundError), \
                mock.patch.object(voice_lock, "_create_lock", side_effect=create_error):
            self.assert_emit(7, lambda: voice_lock.cmd_acquire(args(entry="/tmp/entry")))

    def test_release_and_steal_refusal_branches(self):
        with self.guard_patch(), mock.patch.object(voice_lock, "read_lock", return_value=structured()), \
                mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                mock.patch.object(voice_lock, "pid_start_time_ms", return_value=9_000):
            self.assert_emit(0, lambda: voice_lock.cmd_release(args()))
        with self.guard_patch(), mock.patch.object(voice_lock, "read_lock", return_value={"kind": "legacy", "pid": 101}), \
                mock.patch.object(voice_lock.os, "unlink", side_effect=FileNotFoundError):
            self.assert_emit(0, lambda: voice_lock.cmd_release(args()))
        for lock, expected in [
            ({"kind": "absent"}, 0),
            ({"kind": "unknown"}, 4),
            ({"kind": "legacy", "pid": 202}, 4),
        ]:
            with self.guard_patch(), mock.patch.object(voice_lock, "read_lock", return_value=lock):
                self.assert_emit(expected, lambda: voice_lock.cmd_steal(args()))
        with self.guard_patch(), mock.patch.object(voice_lock, "read_lock", return_value={"kind": "legacy", "pid": 101}), \
                mock.patch.object(voice_lock, "pid_alive", return_value=False), \
                mock.patch.object(voice_lock.os, "unlink", side_effect=FileNotFoundError):
            self.assert_emit(0, lambda: voice_lock.cmd_steal(args()))

    def test_guard_hold_reports_contention_and_read_failure(self):
        with mock.patch.object(voice_lock.os, "open", return_value=9), \
                mock.patch.object(voice_lock.fcntl, "flock", side_effect=OSError):
            self.assert_emit(3, lambda: voice_lock.cmd_guard_hold(args()))
        with mock.patch.object(voice_lock.os, "open", return_value=9), \
                mock.patch.object(voice_lock.fcntl, "flock"), \
                mock.patch.object(voice_lock.sys.stdin, "read", side_effect=OSError):
            self.assert_emit(0, lambda: voice_lock.cmd_guard_hold(args()))

    def test_entry_matching_edge_cases(self):
        self.assertFalse(voice_lock._argv_entry_matches("", "/tmp/voice-agent.py"))
        self.assertTrue(
            voice_lock._argv_entry_matches("python voice-agent.py", "/tmp/voice-agent.py")
        )
        valid, detail = voice_lock._validate_entry(
            "/tmp/voice-agent.py", ["/tmp/voice-agent.py"], ""
        )
        self.assertFalse(valid)
        self.assertIn("not present in live argv", detail)

    def test_termination_error_paths_are_bounded(self):
        def missing(_sig):
            raise ProcessLookupError

        self.assertEqual(
            voice_lock._terminate_and_wait(missing, lambda: False, 0, 0),
            (True, False),
        )

        def gone_on_kill(sig):
            if sig == signal.SIGKILL:
                raise ProcessLookupError

        self.assertEqual(
            voice_lock._terminate_and_wait(gone_on_kill, lambda: False, 0, 0),
            (True, True),
        )

        def denied_on_kill(sig):
            if sig == signal.SIGKILL:
                raise OSError

        with mock.patch.object(voice_lock.time, "monotonic", side_effect=[0, 1, 2, 2, 3]), \
                mock.patch.object(voice_lock.time, "sleep"):
            self.assertEqual(
                voice_lock._terminate_and_wait(denied_on_kill, lambda: False, 0, 100),
                (False, True),
            )

    def test_post_kill_unlink_revalidation(self):
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "absent"}):
            self.assertTrue(voice_lock._unlink_after_revalidate("unused", "vl1-test"))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "structured", "lockId": "other"}):
            self.assertFalse(voice_lock._unlink_after_revalidate("unused", "vl1-test"))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "legacy", "pid": 101}), \
                mock.patch.object(voice_lock.os, "unlink", side_effect=FileNotFoundError):
            self.assertTrue(voice_lock._unlink_after_revalidate("unused", None))

    def test_adopted_takeover_stale_and_identity_refusals(self):
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "unknown"}):
            self.assert_emit(3, lambda: voice_lock._takeover_adopted(args()))
        for unlinked, expected in [(True, 0), (False, 3)]:
            with mock.patch.object(voice_lock, "read_lock", return_value=structured()), \
                    mock.patch.object(voice_lock, "pid_alive", return_value=False), \
                    mock.patch.object(voice_lock, "_unlink_after_revalidate", return_value=unlinked):
                self.assert_emit(expected, lambda: voice_lock._takeover_adopted(args()))
        for unlinked, expected in [(True, 0), (False, 3)]:
            with mock.patch.object(voice_lock, "read_lock", return_value=structured()), \
                    mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                    mock.patch.object(voice_lock, "pid_start_time_ms", return_value=9_000), \
                    mock.patch.object(voice_lock, "_unlink_after_revalidate", return_value=unlinked):
                self.assert_emit(expected, lambda: voice_lock._takeover_adopted(args()))
        with mock.patch.object(voice_lock, "read_lock", return_value=structured()), \
                mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                mock.patch.object(voice_lock, "pid_start_time_ms", return_value=1_000), \
                mock.patch.object(voice_lock, "listener_pids", return_value=[101]), \
                mock.patch.object(voice_lock, "pid_argv", return_value="voice-agent.py"), \
                mock.patch.object(voice_lock, "_validate_entry", return_value=(True, None)):
            self.assert_emit(3, lambda: voice_lock._takeover_adopted(args(workspace="/other")))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "legacy", "pid": 101}), \
                mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                mock.patch.object(voice_lock, "listener_pids", return_value=[101]), \
                mock.patch.object(voice_lock, "pid_argv", return_value="unrelated"):
            self.assert_emit(3, lambda: voice_lock._takeover_adopted(args()))

    def test_adopted_takeover_kill_and_revalidation_failures(self):
        common = [
            mock.patch.object(voice_lock, "read_lock", return_value=structured()),
            mock.patch.object(voice_lock, "pid_alive", return_value=True),
            mock.patch.object(voice_lock, "pid_start_time_ms", return_value=1_000),
            mock.patch.object(voice_lock, "listener_pids", return_value=[101]),
            mock.patch.object(voice_lock, "pid_argv", return_value="voice-agent.py"),
            mock.patch.object(voice_lock, "_validate_entry", return_value=(True, None)),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in common:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(voice_lock, "_terminate_and_wait", return_value=(False, True)))
            self.assert_emit(5, lambda: voice_lock._takeover_adopted(args()))
        with contextlib.ExitStack() as stack:
            for patcher in common:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(voice_lock, "_terminate_and_wait", return_value=(True, False)))
            stack.enter_context(mock.patch.object(voice_lock, "_unlink_after_revalidate", return_value=False))
            self.assert_emit(3, lambda: voice_lock._takeover_adopted(args()))

    def test_owned_takeover_missing_and_stale_roots(self):
        self.assert_emit(4, lambda: voice_lock._takeover_owned(args(pid=None)))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "legacy", "pid": 202}), \
                mock.patch.object(voice_lock, "pid_alive", return_value=False), \
                mock.patch.object(voice_lock, "_unlink_after_revalidate", return_value=True):
            self.assert_emit(0, lambda: voice_lock._takeover_owned(args()))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "absent"}), \
                mock.patch.object(voice_lock, "pid_alive", return_value=False):
            self.assert_emit(0, lambda: voice_lock._takeover_owned(args()))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "unknown", "pid": 202}), \
                mock.patch.object(voice_lock, "pid_alive", side_effect=[False, True]):
            self.assert_emit(3, lambda: voice_lock._takeover_owned(args()))

    def test_owned_takeover_identity_kill_and_revalidation_failures(self):
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "absent"}), \
                mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                mock.patch.object(voice_lock, "pid_start_time_ms", return_value=1_000), \
                mock.patch.object(voice_lock, "pid_argv", return_value="unrelated"):
            self.assert_emit(3, lambda: voice_lock._takeover_owned(args()))
        with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "legacy", "pid": 202}), \
                mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                mock.patch.object(voice_lock, "pid_start_time_ms", return_value=1_000), \
                mock.patch.object(voice_lock, "pid_argv", return_value="voice-agent.py"), \
                mock.patch.object(voice_lock, "_argv_entry_matches", return_value=True), \
                mock.patch.object(voice_lock, "pid_pgid", return_value=999):
            self.assert_emit(3, lambda: voice_lock._takeover_owned(args()))
        for dead, revalidated, expected in [(False, True, 5), (True, False, 3)]:
            with mock.patch.object(voice_lock, "read_lock", return_value={"kind": "absent"}), \
                    mock.patch.object(voice_lock, "pid_alive", return_value=True), \
                    mock.patch.object(voice_lock, "pid_start_time_ms", return_value=1_000), \
                    mock.patch.object(voice_lock, "pid_argv", return_value="voice-agent.py"), \
                    mock.patch.object(voice_lock, "_argv_entry_matches", return_value=True), \
                    mock.patch.object(voice_lock, "_terminate_and_wait", return_value=(dead, False)), \
                    mock.patch.object(voice_lock, "_unlink_after_revalidate", return_value=revalidated):
                self.assert_emit(expected, lambda: voice_lock._takeover_owned(args()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
