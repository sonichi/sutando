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

    def test_fix_defers_with_live_calls_and_rechecks_before_the_kill(self):
        mod = _load()
        with mock.patch.object(mod, "_phone_server_active_calls", return_value=2), \
                mock.patch.object(mod.subprocess, "Popen") as popen:
            self.assertIn("deferred", mod.fix_conversation_server({"status": "stale"}))
            popen.assert_not_called()
        # Idle at the first look, a call starts before the kill: still deferred, nothing killed.
        calls = iter([0, 1])
        runs = []
        with mock.patch.object(mod, "_phone_server_active_calls", lambda: next(calls)), \
                mock.patch.object(mod, "_filter_pids_this_checkout", lambda pids: pids), \
                mock.patch.object(mod.subprocess, "run", lambda argv, **kw: runs.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="777\n", stderr="")), \
                mock.patch.object(mod.subprocess, "Popen") as popen:
            self.assertEqual(mod.fix_conversation_server({"status": "stale"}, settle_s=0), "a call started — deferred")
            self.assertFalse(any(a[0] == "/bin/kill" for a in runs))
            popen.assert_not_called()

    def test_fix_kills_a_stale_idle_server_and_relaunches_the_bundle(self):
        mod = _load()
        runs = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(mod, "REPO_DIR", Path(td)), \
                mock.patch.object(mod, "_phone_server_active_calls", return_value=0), \
                mock.patch.object(mod, "_filter_pids_this_checkout", lambda pids: pids), \
                mock.patch.object(mod.subprocess, "run", lambda argv, **kw: runs.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="777\n888\n", stderr="")), \
                mock.patch.object(mod.subprocess, "Popen") as popen, \
                mock.patch("builtins.open", mock.mock_open()):
            (Path(td) / "dist").mkdir()
            (Path(td) / "dist" / "conversation-server.js").write_text("// bundle")
            self.assertEqual(mod.fix_conversation_server({"status": "stale"}, settle_s=0), "restarted (stale code)")
            killed = [a[1] for a in runs if a[0] == "/bin/kill"]
            self.assertEqual(killed, ["777", "888"])
            argv = popen.call_args[0][0]
            self.assertEqual(argv[0], "node")
            self.assertTrue(argv[1].endswith("dist/conversation-server.js"))
        # A merely down server is relaunched without any kill.
        runs.clear()
        with mock.patch.object(mod, "_phone_server_active_calls", return_value=0), \
                mock.patch.object(mod.subprocess, "run", lambda argv, **kw: runs.append(argv)), \
                mock.patch.object(mod.subprocess, "Popen") as popen, \
                mock.patch("builtins.open", mock.mock_open()):
            self.assertEqual(mod.fix_conversation_server({"status": "warn"}), "restarted")
            self.assertEqual(runs, [])
            popen.assert_called_once()

    def test_fix_still_relaunches_when_the_pid_sweep_fails(self):
        # A sweep that cannot read the process table must not block the relaunch.
        mod = _load()
        with mock.patch.object(mod, "_phone_server_active_calls", return_value=0), \
                mock.patch.object(mod, "_filter_pids_this_checkout", side_effect=OSError("no ps")), \
                mock.patch.object(mod.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="777\n", stderr="")), \
                mock.patch.object(mod.subprocess, "Popen") as popen, \
                mock.patch("builtins.open", mock.mock_open()):
            self.assertEqual(mod.fix_conversation_server({"status": "stale"}, settle_s=0), "restarted (stale code)")
            popen.assert_called_once()

    def test_main_fix_routes_a_stale_phone_server_through_the_repair(self):
        import io
        from contextlib import redirect_stdout
        mod = _load()
        checks = [{"name": "conversation-server", "status": "stale",
                   "detail": "running, but the artifact it executes was rebuilt 40 min after the process started -- restart needed"}]
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(sys, "argv", ["health-check.py", "--fix"]), \
                mock.patch.object(mod, "WORKSPACE_DIR", Path(td)), \
                mock.patch.object(mod, "run_all_checks", return_value=checks), \
                mock.patch.object(mod, "fix_down_bridges", return_value=[]), \
                mock.patch.object(mod, "fix_conversation_server", return_value="restarted (stale code)") as fix, \
                mock.patch.object(mod.subprocess, "Popen"), \
                mock.patch("time.sleep", lambda *_: None):
            try:
                with redirect_stdout(out):
                    mod.main()
            except SystemExit:
                pass
        self.assertIn("conversation-server: restarted (stale code)", out.getvalue())
        fix.assert_called_once_with(checks[0])

    def test_the_conversation_server_check_names_the_artifact(self):
        src = (REPO / "src/health-check.py").read_text()
        i = src.index('mark_stale_if_outdated(\n                    c,\n                    REPO_DIR / "skills" / "phone-conversation"')
        self.assertIn('binary_path=REPO_DIR / "dist" / "conversation-server.js"', src[i:i + 400])
        self.assertIn('print(f"  {c[\'name\']}: {fix_conversation_server(c)}")', src, "the --fix loop routes through fix_conversation_server")


if __name__ == "__main__":
    unittest.main()
