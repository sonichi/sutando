#!/usr/bin/env python3
"""A bridge left running from another install's engine is flagged; read-only.

Field case: the Slack bridge kept running from an older app's engine, so it
wrote into that install's workspace while the core watched the desktop app's.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python3 -I` does not add the script dir

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
ROOT = Path(__file__).resolve().parent.parent
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-bridge-engine-")
_slack_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
(_slack_cfg / "access.json").write_text("{}")
_discord_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_cfg.mkdir(parents=True, exist_ok=True)
(_discord_cfg / "access.json").write_text("{}")
_telegram_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_telegram_cfg.mkdir(parents=True, exist_ok=True)
(_telegram_cfg / "access.json").write_text("{}")
sys.path.insert(0, str(ROOT / "src"))

import bridge_engine  # noqa: E402


def _engine(base: Path, name: str) -> Path:
    eng = base / name / "engine" / "sutando"
    (eng / "src").mkdir(parents=True)
    return eng


class EngineMatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "Application Support"
        self.core = _engine(self.tmp, "desktop")
        self.old = _engine(self.tmp, "old-app")

    def _probe(self, procs: dict, cwds=None, engine=None):
        """`procs`: {pid: argv}, or {pid: (script, argv-or-exception)} for a process
        whose argv does not itself name the script."""
        cwds = cwds or {}
        table = {pid: v if isinstance(v, tuple) else (next(
            (s for s in bridge_engine.BRIDGE_SCRIPTS.values() if s in str(v)), None), v)
            for pid, v in procs.items()}

        def argv_of(pid):
            value = table[pid][1]
            if isinstance(value, Exception):
                raise value
            return value
        return bridge_engine.check_bridge_engine_match(
            engine or self.core,
            lambda script: ([p for p, (s, _) in table.items() if s == script], True),
            argv_of=argv_of, cwd_of=lambda pid: cwds.get(pid, ""))

    def test_same_engine_is_ok(self):
        got = self._probe({"1": ["python3", f"{self.core}/src/slack-bridge.py"]})
        self.assertEqual(got["status"], "ok", got)
        self.assertEqual(got["detail"], "1 bridge(s) run from this engine")

    def test_relative_launch_from_same_engine_is_ok(self):
        got = self._probe({"2": ["python3", "src/telegram-bridge.py"]}, {"2": str(self.core)})
        self.assertEqual(got["status"], "ok", got)

    def test_other_engine_warns_naming_both_dirs(self):
        # Flattened argv with a space in the path, as `ps` prints it.
        got = self._probe({"3": f"python3 {self.old}/src/slack-bridge.py"})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn(f"slack-bridge pid 3 runs from {os.path.realpath(self.old)}", got["detail"])
        self.assertIn(f"not this app's engine ({os.path.realpath(self.core)})", got["detail"])
        self.assertIn("Restart it from the desktop app", got["detail"])

    def test_symlinked_same_engine_is_ok(self):
        link = self.tmp / "engine-link"
        link.symlink_to(self.core)
        got = self._probe({"4": ["python3", f"{link}/src/discord-bridge.py"]})
        self.assertEqual(got["status"], "ok", got)
        got = self._probe({"4": ["python3", f"{self.core}/src/discord-bridge.py"]}, engine=link)
        self.assertEqual(got["status"], "ok", got)

    def test_unreadable_path_is_unknown(self):
        got = bridge_engine.check_bridge_engine_match(
            self.core, lambda s: (["5"] if s == "slack-bridge.py" else [], True),
            argv_of=lambda pid: "", cwd_of=lambda pid: "")
        self.assertEqual(got["status"], "unknown", got)
        self.assertIn("slack-bridge pid 5: script path unreadable", got["detail"])
        got = self._probe({"6": ["python3", "src/slack-bridge.py"]})  # relative, no cwd
        self.assertEqual(got["status"], "unknown", got)
        self.assertIn("relative script path and cwd unreadable", got["detail"])

    def test_bad_process_does_not_hide_a_real_mismatch(self):
        for bad in (RuntimeError("gone"), ValueError("\x00"), [None, 3, {}]):
            procs = {"7": ["python3", f"{self.old}/src/slack-bridge.py"],
                     "8": ("discord-bridge.py", bad)}
            got = self._probe(procs)
            self.assertEqual(got["status"], "warn", (bad, got))
            self.assertIn("slack-bridge pid 7 runs from", got["detail"])
            self.assertIn("Also engine unknown: discord-bridge pid 8", got["detail"])

    def test_bridge_script_as_a_later_argument_is_not_a_bridge(self):
        argv = ["python3", f"{self.core}/src/read-file.py", f"{self.old}/src/slack-bridge.py"]
        got = self._probe({"9": ("slack-bridge.py", argv)})
        self.assertNotEqual(got["status"], "warn", got)
        self.assertIn("slack-bridge pid 9: script path unreadable", got["detail"])
        flat = f"python3 {self.core}/src/read-file.py {self.old}/src/slack-bridge.py"
        got = self._probe({"9": ("slack-bridge.py", flat)})
        self.assertNotEqual(got["status"], "warn", got)

    def test_interpreter_options_are_skipped(self):
        argv = ["/usr/bin/python3", "-I", "-u", "-X", "utf8", "-W", "ignore",
                f"{self.old}/src/discord-bridge.py"]
        got = self._probe({"10": argv})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("discord-bridge pid 10 runs from", got["detail"])

    def test_unbuffered_launch_with_spaces_in_both_paths_is_recognised(self):
        interp = f"{self.tmp}/runtime/python/bin/python3"  # self.tmp contains "Application Support"
        for argv in ([interp, "-u", f"{self.old}/src/slack-bridge.py"],
                     f"{interp} -u {self.old}/src/slack-bridge.py"):
            got = self._probe({"13": ("slack-bridge.py", argv)})
            self.assertEqual(got["status"], "warn", (argv, got))
            self.assertIn(f"runs from {os.path.realpath(self.old)}", got["detail"])
        got = self._probe({"13": ("slack-bridge.py", [interp, "-u", f"{self.core}/src/slack-bridge.py"])})
        self.assertEqual(got["status"], "ok", got)

    def test_execution_modes_are_not_script_launches(self):
        for argv in (["python3", "-c", "import x", f"{self.old}/src/slack-bridge.py"],
                     ["python3", "-m", "pkg", f"{self.old}/src/slack-bridge.py"],
                     f"python3 -m pkg {self.old}/src/slack-bridge.py",
                     ["python3", "-cpass", f"{self.old}/src/slack-bridge.py"],
                     ["python3", "-mpkg", f"{self.old}/src/slack-bridge.py"],
                     ["python3", "-Icpass", f"{self.old}/src/slack-bridge.py"],
                     f"python3 -cpass {self.old}/src/slack-bridge.py"):
            got = self._probe({"14": ("slack-bridge.py", argv)})
            self.assertEqual(got["status"], "unknown", (argv, got))

    def test_no_bridge(self):
        got = self._probe({})
        self.assertEqual(got, {"name": "bridge-engine-match", "status": "ok",
                               "detail": "no bridge running"})


class Readers(unittest.TestCase):
    def test_operand_edge_cases(self):
        op = bridge_engine._script_operand
        self.assertIsNone(op(["vim", "/e/src/slack-bridge.py"], "slack-bridge.py"))
        self.assertIsNone(op(["python3", "-", "/e/src/slack-bridge.py"], "slack-bridge.py"))
        self.assertIsNone(op(["python3", "-u"], "slack-bridge.py"))
        self.assertIsNone(op(["python3", "-u", "-X"], "slack-bridge.py"))
        self.assertEqual(op(["python3", "--", "/e/src/slack-bridge.py"], "slack-bridge.py"),
                         "/e/src/slack-bridge.py")
        self.assertIsNone(op(["python3", "--"], "slack-bridge.py"))
        self.assertIsNone(op(["python3", 3, "/e/src/slack-bridge.py"], "slack-bridge.py"))
        self.assertIsNone(op("vim /e/src/slack-bridge.py", "slack-bridge.py"))
        self.assertIsNone(op("python3 -u", "slack-bridge.py"))

    def test_read_argv_prefers_the_vector_then_ps(self):
        me = str(os.getpid())
        with mock.patch.object(bridge_engine, "_argv_vector", lambda pid: ["python3", "x.py"]):
            self.assertEqual(bridge_engine.read_argv(me), ["python3", "x.py"])
        ps = mock.Mock(stdout="python3 /e/src/slack-bridge.py\n")
        with mock.patch.object(bridge_engine, "_argv_vector", lambda pid: None), \
             mock.patch.object(bridge_engine.subprocess, "run", return_value=ps) as run:
            self.assertEqual(bridge_engine.read_argv(me), "python3 /e/src/slack-bridge.py")
        self.assertEqual(run.call_args.args[0][:3], ["/bin/ps", "-o", "command="])
        for exc in (OSError("no ps"), bridge_engine.subprocess.TimeoutExpired("ps", 5)):
            with mock.patch.object(bridge_engine, "_argv_vector", lambda pid: None), \
                 mock.patch.object(bridge_engine.subprocess, "run", side_effect=exc):
                self.assertEqual(bridge_engine.read_argv(me), "")

    def test_read_cwd_from_proc(self):
        root = Path(tempfile.mkdtemp())
        (root / "41").mkdir()
        (root / "41" / "cwd").symlink_to("/some/engine")
        self.assertEqual(bridge_engine.read_cwd("41", proc_root=str(root)), "/some/engine")
        (root / "42").mkdir()
        (root / "42" / "cwd").write_text("not a link")
        self.assertEqual(bridge_engine.read_cwd("42", proc_root=str(root)), "")

    def test_read_cwd_from_lsof(self):
        absent = str(Path(tempfile.mkdtemp()) / "no-proc")
        out = mock.Mock(stdout="p41\nfcwd\nn/some/engine\n")
        with mock.patch.object(bridge_engine.subprocess, "run", return_value=out) as run:
            self.assertEqual(bridge_engine.read_cwd("41", proc_root=absent), "/some/engine")
        self.assertEqual(run.call_args.args[0][0], "/usr/sbin/lsof")
        with mock.patch.object(bridge_engine.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.assertEqual(bridge_engine.read_cwd("41", proc_root=absent), "")
        for exc in (OSError("no lsof"), bridge_engine.subprocess.TimeoutExpired("lsof", 5)):
            with mock.patch.object(bridge_engine.subprocess, "run", side_effect=exc):
                self.assertEqual(bridge_engine.read_cwd("41", proc_root=absent), "")


class HealthCheckWiring(unittest.TestCase):
    def test_a_crashing_probe_is_unknown_not_ok(self):
        health = self._health()
        with mock.patch.object(bridge_engine, "check_bridge_engine_match",
                               side_effect=RuntimeError("boom")):
            got = health.check_bridge_engine_match()
        self.assertEqual(got, {"name": "bridge-engine-match", "status": "unknown",
                               "detail": "probe failed (RuntimeError)"})

    def _health(self):
        spec = importlib.util.spec_from_file_location("health_check", ROOT / "src" / "health-check.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        return health

    def test_failed_scan_is_unknown_not_absence(self):
        health = self._health()
        for scanned, status in ((True, "ok"), (False, "unknown")):
            with mock.patch.object(health, "probe_pids", lambda pat, _s=scanned: ([], _s)):
                got = health.check_bridge_engine_match()
            self.assertEqual(got["status"], status, (scanned, got))
        self.assertIn("slack-bridge: process scan failed", got["detail"])

    def test_failed_scan_does_not_hide_a_mismatch_from_a_good_scan(self):
        health = self._health()
        other = Path(tempfile.mkdtemp()) / "other-engine"
        argv = ["python3", f"{other}/src/telegram-bridge.py"]
        with mock.patch.object(health, "probe_pids", lambda pat: (
                (["12"], True) if "telegram" in pat else ([], "slack" not in pat))), \
             mock.patch.object(health, "_drop_launcher_parents", lambda p: p), \
             mock.patch.object(bridge_engine, "read_argv", lambda pid: argv), \
             mock.patch.object(bridge_engine, "read_cwd", lambda pid: ""):
            got = health.check_bridge_engine_match()
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("telegram-bridge pid 12 runs from", got["detail"])
        self.assertIn("Also engine unknown: slack-bridge: process scan failed", got["detail"])

    def test_health_check_compares_with_its_own_engine(self):
        spec = importlib.util.spec_from_file_location("health_check", ROOT / "src" / "health-check.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        other = Path(tempfile.mkdtemp()) / "other-engine"
        argvs = {"11": ["python3", f"{ROOT}/src/slack-bridge.py"],
                 "12": ["python3", f"{other}/src/telegram-bridge.py"]}
        with mock.patch.object(health, "probe_pids", lambda pat: (
                [p for p, a in argvs.items() if a[1].rsplit("/", 1)[1].split("-")[0] in pat], True)), \
             mock.patch.object(health, "_drop_launcher_parents", lambda p: p), \
             mock.patch.object(bridge_engine, "read_argv", lambda pid: argvs[pid]), \
             mock.patch.object(bridge_engine, "read_cwd", lambda pid: ""):
            got = health.check_bridge_engine_match()
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("telegram-bridge pid 12 runs from", got["detail"])
        self.assertNotIn("slack-bridge pid 11", got["detail"])
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_bridge_engine_match())", src)


if __name__ == "__main__":
    unittest.main()
