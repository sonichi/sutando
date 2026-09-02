"""Contract for src/tmux_probe.has_session: True / False / None.

The stderr strings below are verbatim from tmux 3.6a (server, homebrew) and a
3.5a client (the desktop app's vendored engine/bin/tmux) against the same
socket: the two genuine misses stay False, the version-skew failure is None.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import tmux_probe  # noqa: E402


class _R:
    def __init__(self, rc, stderr=b""):
        self.returncode = rc
        self.stderr = stderr


class TestClassify(unittest.TestCase):
    def test_present(self):
        self.assertIs(tmux_probe.classify(0, b""), True)

    def test_unknown_session_is_absent(self):
        self.assertIs(tmux_probe.classify(1, b"can't find session: sutando-core\n"), False)

    def test_no_server_on_socket_is_absent(self):
        err = b"error connecting to /tmp/x.sock (No such file or directory)\n"
        self.assertIs(tmux_probe.classify(1, err), False)

    def test_bare_nonzero_without_stderr_is_absent(self):
        # A subprocess double with no stderr at all must read as an ordinary miss.
        self.assertIs(tmux_probe.classify(1, None), False)
        self.assertIs(tmux_probe.classify(1, ""), False)

    def test_version_skew_client_is_unknown(self):
        self.assertIsNone(tmux_probe.classify(1, b"server exited unexpectedly\n"))
        self.assertIsNone(tmux_probe.classify(1, "protocol version mismatch (client 8, server 9)"))
        self.assertIsNone(tmux_probe.classify(1, b"lost server\n"))

    def test_client_fault_never_reads_true(self):
        # rc 0 wins outright; the signatures only demote a non-zero exit.
        self.assertIs(tmux_probe.classify(0, b"server exited unexpectedly\n"), True)

    def test_unexecuted_is_unknown(self):
        self.assertIsNone(tmux_probe.classify(None, b""))


class TestHasSession(unittest.TestCase):
    """Real body with subprocess.run stubbed on the shared module."""

    def _with_run(self, fake, **kw):
        orig = subprocess.run
        subprocess.run = fake
        try:
            return tmux_probe.has_session("s.sock", "core", **kw)
        finally:
            subprocess.run = orig

    def test_argv_and_timeout_reach_subprocess(self):
        seen = {}

        def fake(argv, **k):
            seen["argv"], seen["timeout"] = argv, k.get("timeout")
            return _R(0)
        self.assertIs(self._with_run(fake, timeout=8, tmux="/x/tmux"), True)
        self.assertEqual(seen["argv"], ["/x/tmux", "-S", "s.sock", "has-session", "-t", "core"])
        self.assertEqual(seen["timeout"], 8)

    def test_skew_stderr_is_unknown(self):
        self.assertIsNone(self._with_run(lambda *a, **k: _R(1, b"server exited unexpectedly\n")))

    def test_miss_is_false(self):
        self.assertIs(self._with_run(lambda *a, **k: _R(1, b"can't find session: core\n")), False)

    def test_double_without_stderr_is_false(self):
        class Bare:
            returncode = 1
        self.assertIs(self._with_run(lambda *a, **k: Bare()), False)

    def test_missing_binary_is_unknown(self):
        def boom(*a, **k):
            raise OSError("no tmux")
        self.assertIsNone(self._with_run(boom))

    def test_timeout_is_unknown(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)
        self.assertIsNone(self._with_run(boom))

    def test_real_binary_missing_is_unknown(self):
        self.assertIsNone(tmux_probe.has_session("s.sock", "core", tmux="/nonexistent-tmux-xyz"))


if __name__ == "__main__":
    unittest.main()
