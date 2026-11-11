#!/usr/bin/env python3
"""A bridge left running from another install's engine is flagged; read-only.

Field case: the Slack bridge kept running from an older app's engine, so it
wrote into that install's workspace while the core watched the desktop app's.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python3 -I` does not add the script dir

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

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
        got = self._probe({"3": ["python3", f"{self.old}/src/slack-bridge.py"]})
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
        self.assertIn("slack-bridge pid 5: argv vector unreadable", got["detail"])
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
        self.assertIn("slack-bridge pid 9: the interpreter's first script operand "
                      "is not slack-bridge.py", got["detail"])
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
        got = self._probe({"13": ("slack-bridge.py", [interp, "-u", f"{self.old}/src/slack-bridge.py"])})
        self.assertEqual(got["status"], "warn", got)
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

    def test_flattened_ps_line_is_unknown_not_parsed(self):
        for flat in (f"python3 {self.old}/src/slack-bridge.py",
                     f"python3 {self.core}/src/slack-bridge.py", f"python3 {self.old}/x.py python3 y"):
            got = self._probe({"15": ("slack-bridge.py", flat)})
            self.assertEqual(got["status"], "unknown", (flat, got))
            self.assertIn("slack-bridge pid 15: argv vector unreadable (a flattened ps command "
                          "line is ambiguous, so it is not parsed)", got["detail"])
        got = self._probe({"15": ("slack-bridge.py", [])})
        self.assertEqual(got["status"], "unknown", got)

    def test_bridge_with_trailing_arguments_is_recognised(self):
        got = self._probe({"16": ["python3", "-u", f"{self.old}/src/slack-bridge.py", "--foo", "x"]})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("slack-bridge pid 16 runs from", got["detail"])
        got = self._probe({"16": ["python3", f"{self.core}/src/slack-bridge.py", "--foo"]})
        self.assertEqual(got, {"name": "bridge-engine-match", "status": "ok",
                               "detail": "1 bridge(s) run from this engine"})

    def test_only_python_interpreters_count(self):
        for tool in ("vim", "/usr/bin/tail", "less", "pythonista", "python2", "ipython3"):
            got = self._probe({"17": [tool, f"{self.old}/src/slack-bridge.py"]})
            self.assertEqual(got, {"name": "bridge-engine-match", "status": "ok",
                                   "detail": "no bridge running"}, tool)
        for interp in ("python", "python3", "/opt/py/bin/python3.12", "Python"):
            got = self._probe({"17": [interp, f"{self.old}/src/slack-bridge.py"]})
            self.assertEqual(got["status"], "warn", (interp, got))
        got = self._probe({"17": ["vim", f"{self.core}/src/slack-bridge.py"],
                           "18": ["python3", f"{self.core}/src/slack-bridge.py"]})
        self.assertEqual(got["detail"], "1 bridge(s) run from this engine")

    def test_case_variant_of_the_engine_does_not_warn_on_a_case_insensitive_volume(self):
        variant = Path(str(self.core).replace("/desktop/", "/DESKTOP/"))
        argv = ["python3", f"{variant}/src/slack-bridge.py"]
        for insensitive, status in ((True, "ok"), (False, "warn")):
            with mock.patch.object(bridge_engine.os.path, "samefile",
                                   lambda a, b, _r=insensitive: _r):
                got = self._probe({"19": argv})
            self.assertEqual(got["status"], status, (insensitive, got))
        with mock.patch.object(bridge_engine.os.path, "samefile", side_effect=OSError("gone")):
            self.assertEqual(self._probe({"19": argv})["status"], "warn")
        with mock.patch.object(bridge_engine.os.path, "samefile", side_effect=AssertionError):
            got = self._probe({"19": ["python3", f"{self.old}/src/slack-bridge.py"]})
        self.assertEqual(got["status"], "warn", got)  # spellings that differ beyond case

    def test_case_variant_against_the_real_volume(self):
        variant = Path(str(self.core).replace("/desktop/", "/DESKTOP/"))
        insensitive = variant.exists()
        got = self._probe({"20": ["python3", f"{variant}/src/slack-bridge.py"]})
        self.assertEqual(got["status"], "ok" if insensitive else "warn", (insensitive, got))

    def test_windows_is_unknown_and_says_why(self):
        def never(script):
            raise AssertionError("no scan on Windows")
        got = bridge_engine.check_bridge_engine_match(self.core, never, platform="win32")
        self.assertEqual(got, {"name": "bridge-engine-match", "status": "unknown",
                               "detail": "not checked on Windows: there is no reader here for a "
                                         "process's argv vector or working directory"})

    def test_no_bridge(self):
        got = self._probe({})
        self.assertEqual(got, {"name": "bridge-engine-match", "status": "ok",
                               "detail": "no bridge running"})


class Readers(unittest.TestCase):
    def test_operand_edge_cases(self):
        op = bridge_engine._first_operand
        self.assertIsNone(op(["python3", "-", "/e/src/slack-bridge.py"]))
        self.assertIsNone(op(["python3", "-u"]))
        self.assertIsNone(op(["python3", "-u", "-X"]))
        self.assertEqual(op(["python3", "--", "/e/src/slack-bridge.py"]), "/e/src/slack-bridge.py")
        self.assertIsNone(op(["python3", "--"]))
        self.assertEqual(op(["python3", "/e/src/slack-bridge.py", "--foo"]), "/e/src/slack-bridge.py")

    def test_read_argv_is_the_vector_and_never_ps(self):
        me = str(os.getpid())
        with mock.patch.object(bridge_engine.subprocess, "run", side_effect=AssertionError("ps")):
            with mock.patch.object(bridge_engine, "_argv_vector", lambda pid: ["python3", "x.py"]):
                self.assertEqual(bridge_engine.read_argv(me), ["python3", "x.py"])
            for none in (None, []):
                with mock.patch.object(bridge_engine, "_argv_vector", lambda pid, _n=none: _n):
                    self.assertIsNone(bridge_engine.read_argv(me))
        vec = bridge_engine.read_argv(me)
        self.assertIsInstance(vec, list)
        self.assertIn(Path(__file__).name, " ".join(vec))

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

    def test_scan_pattern_finds_a_bridge_with_trailing_arguments(self):
        health = self._health()
        seen = []
        with mock.patch.object(health, "probe_pids", lambda pat: (seen.append(pat), ([], True))[1]):
            health.check_bridge_engine_match()
        pattern = next(p for p in seen if "slack" in p)
        pgrep = shutil.which("pgrep")
        if pgrep is None:
            self.assertTrue(sys.platform.startswith("win"), "pgrep missing on a POSIX host")
            return
        src = Path(tempfile.mkdtemp()) / "src"
        src.mkdir()
        sleeper = "import time; time.sleep(30)"
        procs = [subprocess.Popen([sys.executable, "-c", sleeper, *tail], env=isolated_child_env())
                 for tail in ([f"{src}/slack-bridge.py", "--foo"], [f"{src}/slack-bridge.py"],
                              [f"{src}/slack-bridge.py.log"])]
        try:
            out = subprocess.run([pgrep, "-f", pattern], capture_output=True, text=True,
                                 env=isolated_child_env(), timeout=10).stdout.split()
        finally:
            for p in procs:
                p.kill()
                p.wait()
        self.assertIn(str(procs[0].pid), out)
        self.assertIn(str(procs[1].pid), out)
        self.assertNotIn(str(procs[2].pid), out)

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
