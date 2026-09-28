#!/usr/bin/env python3
"""health-check.py's phone-server pieces from P1-19: `ps -o lstart` is read under
LC_ALL=C (a non-English locale broke the English strptime), a stale server is
never restarted while it has live calls, and the bundled artifact is launched
when it exists.

Run: python3 tests/health-check-phone-server-restart-safety.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_phone", REPO / "src/health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.argv = ["health-check.py"]
    spec.loader.exec_module(mod)
    return mod


class TestPsLocale(unittest.TestCase):
    def test_ps_lstart_runs_under_the_c_locale(self):
        mod = _load()
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw.get("env")))
            if argv[0].endswith("pgrep"):
                return subprocess.CompletedProcess(argv, 0, stdout="4242\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="4242 Mon Sep 28 10:00:00 2026\n", stderr="")

        with mock.patch.object(mod.subprocess, "run", fake_run), \
                mock.patch.object(mod, "_filter_pids_this_checkout", lambda pids: pids):
            starts, by_pid = mod._proc_lstarts("conversation-server")
        self.assertEqual(len(starts), 1)
        self.assertEqual(by_pid, {"4242": "Mon Sep 28 10:00:00 2026"})
        ps_calls = [env for argv, env in seen if argv[0] == "/bin/ps"]
        self.assertEqual(len(ps_calls), 1)
        self.assertEqual(ps_calls[0].get("LC_ALL"), "C")


class TestRestartSafety(unittest.TestCase):
    def test_active_calls_come_from_health_and_default_to_zero(self):
        mod = _load()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", lambda url, timeout=0: _Resp(json.dumps({"activeCalls": 2}).encode())):
            self.assertEqual(mod._phone_server_active_calls(), 2)
        with mock.patch("urllib.request.urlopen", lambda url, timeout=0: _Resp(json.dumps({"activeCalls": 0}).encode())):
            self.assertEqual(mod._phone_server_active_calls(), 0)
        with mock.patch("urllib.request.urlopen", lambda url, timeout=0: _Resp(b"not json")):
            self.assertEqual(mod._phone_server_active_calls(), 0)

        def down(url, timeout=0):
            raise OSError("refused")
        with mock.patch("urllib.request.urlopen", down):
            self.assertEqual(mod._phone_server_active_calls(), 0, "a server that does not answer has nothing to protect")

    def test_the_bundled_artifact_is_launched_when_present(self):
        mod = _load()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(mod, "REPO_DIR", Path(td)):
                self.assertEqual(mod._phone_server_launch_argv()[:2], ["npx", "tsx"])
                (Path(td) / "dist").mkdir()
                (Path(td) / "dist" / "conversation-server.js").write_text("// bundle")
                argv = mod._phone_server_launch_argv()
                self.assertEqual(argv[0], "node")
                self.assertTrue(argv[1].endswith("dist/conversation-server.js"))

    def test_the_conversation_server_check_names_the_artifact(self):
        src = (REPO / "src/health-check.py").read_text()
        i = src.index('mark_stale_if_outdated(\n                    c,\n                    REPO_DIR / "skills" / "phone-conversation"')
        self.assertIn('binary_path=REPO_DIR / "dist" / "conversation-server.js"', src[i:i + 400])
        self.assertIn("stale but {_live} call(s) active", src, "the --fix path defers a stale server with live calls")


if __name__ == "__main__":
    unittest.main()
