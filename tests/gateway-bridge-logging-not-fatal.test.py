#!/usr/bin/env python3
"""A dead log pipe must not kill the gateway's poll loop.

`remote-gateway-bridge` runs with stdout piped to `tee` (measured on a live
core: fd1/fd2 are PIPE, unlike telegram/discord/voice-agent which are REG). Its
`_log()` does an unguarded `print(..., flush=True)`, and the poll loop's last
resort is `except Exception  # keep the loop alive` — which cannot help, because
**every handler calls `_log()` first**. A `BrokenPipeError` from that print is
raised inside the handler and escapes it, so the loop dies silently: stderr is
the same dead pipe, so nothing is reported.

Same class as #2856 (merged) in `src/discord-bridge.py`; this pins the analogous
guard for the standalone ag2-sparrow package.

`test_a_write_to_a_dead_pipe_does_not_raise` fails on the parent commit.
"""
import importlib
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"


class _DeadPipe:
    """Stands in for a pipe whose reader has gone away."""

    def __init__(self):
        self.writes = 0

    def write(self, data):
        self.writes += 1
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def _never_fatal():
    """Return the module's guard class, importing the module the way the repo's
    other gateway tests do (`tests/gateway-owner-presence-peer-gate.test.py`).

    Importing rather than exec-ing a source slice matters twice over: it is the
    established harness, and it is what makes the guard's lines register as
    covered — `compile()` on a slice renumbers from 1, so diff-cover would credit
    the wrong lines of this file entirely.

    Returns None when the guard is absent (parent commit), so the control arms
    fail on an assertion rather than an ImportError.
    """
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
    # The module installs the guard on the REAL sys.stdout/sys.stderr at import.
    # Restore them, or every later test in the process writes through the wrapper.
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        m = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
        m = importlib.reload(m)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    return getattr(m, "_NeverFatalStream", None)


class GatewayLoggingNotFatalTest(unittest.TestCase):
    # ---- THE regression pin: fails on the parent ---------------------------

    def test_a_write_to_a_dead_pipe_does_not_raise(self):
        """This is the whole defect: the print inside an except handler."""
        cls = _never_fatal()
        if cls is None:
            self.fail("no _NeverFatalStream guard: a dead pipe still kills the poll loop")
        stream = cls(_DeadPipe())
        stream.write("[remote-gateway-bridge] poll network error\n")  # must not raise
        stream.flush()                                                # must not raise

    def test_the_guard_is_actually_installed_on_stdout_and_stderr(self):
        """A class nobody installs protects nothing.

        Asserts on booleans, not `assertIn` against the source: a failure there
        dumps the whole 2400-line module into the report and buries the reason.
        """
        src = MOD.read_text()
        self.assertTrue("sys.stdout = _NeverFatalStream(sys.stdout)" in src,
                        "stdout is never wrapped, so _log still writes raw")
        self.assertTrue("sys.stderr = _NeverFatalStream(sys.stderr)" in src,
                        "stderr is never wrapped, so a crash report dies too")

    def test_it_is_installed_before_log_is_defined(self):
        """Order matters: _log must emit through the guarded stream.

        Uses find() rather than index() so the parent commit FAILS this with a
        readable reason instead of raising ValueError — an error proves a string
        is absent; only a comparison shows the ordering is wrong.
        """
        src = MOD.read_text()
        install, log = src.find("sys.stdout = _NeverFatalStream"), src.find("def _log(msg: str)")
        self.assertNotEqual(install, -1, "guard is never installed on stdout")
        self.assertNotEqual(log, -1, "_log not found — module shape changed")
        self.assertLess(install, log, "_log is defined before the guard is installed")

    # ---- must NOT over-swallow ---------------------------------------------

    def test_only_oserror_is_swallowed(self):
        """Masking every exception would hide real bugs, not just EPIPE."""
        cls = _never_fatal()
        if cls is None:
            self.skipTest("guard absent on this commit")

        class _Nasty:
            def write(self, data):
                raise ValueError("a real bug, not a broken pipe")

            def flush(self):
                raise ValueError("a real bug, not a broken pipe")

        with self.assertRaises(ValueError):
            cls(_Nasty()).write("x")
        with self.assertRaises(ValueError):
            cls(_Nasty()).flush()

    def test_write_reports_the_full_length_so_callers_do_not_branch(self):
        cls = _never_fatal()
        if cls is None:
            self.skipTest("guard absent on this commit")
        data = "12345"
        self.assertEqual(cls(_DeadPipe()).write(data), len(data))

    def test_a_healthy_stream_is_passed_through_untouched(self):
        cls = _never_fatal()
        if cls is None:
            self.skipTest("guard absent on this commit")

        class _Ok:
            def __init__(self):
                self.buf = []

            def write(self, data):
                self.buf.append(data)
                return len(data)

            def flush(self):
                self.buf.append("<flush>")

            encoding = "utf-8"

        ok = _Ok()
        w = cls(ok)
        w.write("hello")
        w.flush()
        self.assertEqual(ok.buf, ["hello", "<flush>"])
        self.assertEqual(w.encoding, "utf-8")  # __getattr__ passthrough


if __name__ == "__main__":
    unittest.main(verbosity=2)
