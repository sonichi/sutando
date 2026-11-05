#!/usr/bin/env python3
"""The launchers' compat gate end to end: real bash, real module, stub CLIs on PATH.

Covers what only the shell glue can get wrong: a healthy host keeps launching
the same command, a fallback install is handed to the launcher by path, an
unusable CLI stops the launch with the fix and the resume command, consent
lets the assisted repair run and the same launch continue, and a logged-out
Codex stops until `codex login`. The last case runs the real Claude launcher
to prove a restart with an unusable CLI never kills the running core.

Run: python3 tests/runtime-compat-gate.test.py
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE_FLAGS = ("--name --dangerously-skip-permissions --add-dir --settings --remote-control "
                "--chrome --plugin-dir --resume --session-id")
CODEX_FLAGS = "--cd --add-dir --sandbox --ask-for-approval --search --no-alt-screen --config --model"


def write_exe(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def fake_cli(path: Path, version: str, flags: str, login_rc: int = 0) -> str:
    return write_exe(path, f"""case "$1" in
  --version) echo "{version}" ;;
  --help) echo "{flags}" ;;
  login) [ -f "$LOGIN_FLAG" ] && exit 0; exit {login_rc} ;;
esac
""")


def launcher_copy(tmp: Path) -> Path:
    """A copy of the code the Claude launcher runs; its workspace, and any owner DM, stay in tmp."""
    repo = tmp / "repo"
    for rel in ("src", "scripts"):
        shutil.copytree(REPO / rel, repo / rel, symlinks=True)
    shutil.copy2(REPO / "sutando.config.json", repo / "sutando.config.json")
    return repo


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        for rel in ("src/runtime_compat.py", "src/auth_preflight.py",
                    "src/agent/runtime-compat-gate.sh", "scripts/python-binary.sh",
                    "src/file_lock.py", "src/local_record.py"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, self.repo / rel)
        self.bin1, self.bin2 = self.tmp / "bin1", self.tmp / "bin2"
        self.bin1.mkdir()
        self.bin2.mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "PATH": f"{self.bin1}:{self.bin2}:/usr/bin:/bin",
                    "SUTANDO_RUNTIMES_DIR": str(self.tmp / "runtimes"),
                    "SUTANDO_RUNTIME_SYSTEM_DIRS": "", "SUTANDO_RUNTIME_WAIT_S": "0",
                    "LOGIN_FLAG": str(self.tmp / "logged-in")}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gate(self, runtime="claude", *args, **env):
        script = ('. "$1/src/agent/runtime-compat-gate.sh"; rc=0; '
                  'sutando_runtime_compat_gate "$1" "$2" "${@:3}" || rc=$?; '
                  'echo "rc=$rc bin=$SUTANDO_CORE_CLI_BIN"')
        r = subprocess.run(["bash", "-c", script, "gate", str(self.repo), runtime, *args],
                           env=dict(self.env, **env), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=120)
        return r.stdout.strip().splitlines()[-1], r.stderr

    def test_healthy_host_keeps_the_same_command(self):
        fake_cli(self.bin1 / "claude", "2.1.286", CLAUDE_FLAGS)
        self.assertEqual(self.gate()[0], "rc=0 bin=claude")

    def test_fallback_install_is_launched_by_path(self):
        write_exe(self.bin1 / "claude", "exit 1\n")
        good = fake_cli(self.bin2 / "claude", "2.1.0", CLAUDE_FLAGS)
        self.assertEqual(self.gate()[0], f"rc=0 bin={good}")

    def test_missing_cli_stops_with_fix_and_resume_command(self):
        line, err = self.gate()
        self.assertEqual(line, "rc=3 bin=claude")
        self.assertIn("fix: curl -fsSL https://claude.ai/install.sh | bash", err)
        self.assertIn("runtime_compat.py repair --runtime claude --yes", err)
        self.assertIn(f"Then run again: bash {self.repo}/src/agent/start-cli.sh", err)

    def test_consent_runs_the_repair_and_the_launch_continues(self):
        write_exe(self.bin1 / "npm", f"""PREFIX=""
while [ $# -gt 0 ]; do [ "$1" = --prefix ] && PREFIX="$2"; shift; done
mkdir -p "$PREFIX/bin"
printf '#!/bin/sh\\ncase "$1" in --version) echo 2.4.0;; --help) echo "{CLAUDE_FLAGS}";; esac\\n' > "$PREFIX/bin/claude"
chmod +x "$PREFIX/bin/claude"
""")
        line, err = self.gate(SUTANDO_RUNTIME_REPAIR_CONSENT="1")
        self.assertEqual(line, f"rc=0 bin={self.tmp / 'runtimes' / 'bin' / 'claude'}", err)
        self.assertIn("repair succeeded", err)

    def test_codex_logged_out_stops_then_login_lets_it_start(self):
        fake_cli(self.bin1 / "codex", "codex-cli 0.160.1", CODEX_FLAGS, login_rc=1)
        line, err = self.gate("codex", "--enforce-auth")
        self.assertEqual(line, "rc=4 bin=codex")
        self.assertIn("not authenticated", err)
        self.assertIn("codex login", err)
        (self.tmp / "logged-in").write_text("")
        self.assertEqual(self.gate("codex", "--enforce-auth")[0], "rc=0 bin=codex")

    def test_skip_switch_and_missing_module_fail_open(self):
        self.assertEqual(self.gate(SUTANDO_SKIP_RUNTIME_COMPAT="1")[0], "rc=0 bin=claude")
        (self.repo / "src/runtime_compat.py").unlink()
        self.assertEqual(self.gate()[0], "rc=0 bin=claude")

    def test_no_python_skips_the_check(self):
        (self.repo / "scripts/python-binary.sh").write_text("resolve_python() { :; }\n")
        line, err = self.gate()
        self.assertEqual(line, "rc=0 bin=claude")
        self.assertIn("version check was skipped", err)


class ClaudeRestartKeepsRunningCore(unittest.TestCase):
    """A --restart whose replacement CLI cannot run must not kill the live core."""

    def test_restart_with_unusable_cli_kills_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = tmp / "bin"
        log = tmp / "tmux.log"
        write_exe(bin_dir / "tmux", f'echo "$*" >> "{log}"\nexit 0\n')
        write_exe(bin_dir / "claude", "exit 1\n")
        write_exe(bin_dir / "pgrep", "exit 1\n")
        env = {"HOME": str(tmp / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin",
               "SUTANDO_RUNTIMES_DIR": str(tmp / "runtimes"), "SUTANDO_RUNTIME_SYSTEM_DIRS": "",
               "SUTANDO_RUNTIME_WAIT_S": "0", "SUTANDO_TMUX_SOCKET": str(tmp / "sock"),
               "SUTANDO_SKIP_AUTH_PREFLIGHT": "1", "CLAUDE_CONFIG_DIR": str(tmp / "cfg"),
               "SUTANDO_RESTART_GRACE_S": "1"}
        repo = launcher_copy(tmp)
        r = subprocess.run(["bash", str(repo / "src/agent/claude/cli/start-cli.sh"), "--restart"],
                           env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=180)
        calls = log.read_text() if log.exists() else ""
        self.assertNotIn("kill-session", calls)
        self.assertNotIn("new-session", calls)
        self.assertEqual(r.returncode, 3, r.stderr[-2000:])
        self.assertIn("cannot start the core", r.stderr)
        dms = list((repo / "workspace" / "results").glob("proactive-runtime-compat-claude-*.txt"))
        self.assertEqual(len(dms), 1, "off a terminal the owner is asked once, in the test's workspace")




class AppLaunchedCoreNeverWaitsOrDms(unittest.TestCase):
    """The real Claude launcher under the app's consent surface: no 600 s wait, no owner DM."""

    def test_app_surface_launch_stops_at_once_without_a_dm(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = tmp / "bin"
        log = tmp / "tmux.log"
        write_exe(bin_dir / "tmux", f'echo "$*" >> "{log}"\nexit 0\n')
        write_exe(bin_dir / "claude", "exit 1\n")
        write_exe(bin_dir / "pgrep", "exit 1\n")
        env = {"HOME": str(tmp / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin",
               "SUTANDO_RUNTIMES_DIR": str(tmp / "runtimes"), "SUTANDO_RUNTIME_SYSTEM_DIRS": "",
               "SUTANDO_TMUX_SOCKET": str(tmp / "sock"), "SUTANDO_SKIP_AUTH_PREFLIGHT": "1",
               "CLAUDE_CONFIG_DIR": str(tmp / "cfg"), "SUTANDO_RUNTIME_CONSENT_SURFACE": "app"}
        repo = launcher_copy(tmp)
        try:
            r = subprocess.run(["bash", str(repo / "src/agent/claude/cli/start-cli.sh")], env=env,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("the launch was still waiting for a fix after 60s")
        self.assertEqual(r.returncode, 3, r.stderr[-2000:])
        self.assertNotIn("waiting up to", r.stderr)
        self.assertNotIn("new-session", log.read_text() if log.exists() else "")
        dms = list((repo / "workspace" / "results").glob("proactive-runtime-compat-claude-*.txt"))
        self.assertEqual(dms, [], "the app shows the card; the gate sends no DM")


class ClaudeConsentRepairLaunchesTheRepairedBinary(unittest.TestCase):
    """The real Claude launcher: consent repairs a broken claude and launches the new one."""

    def test_consent_repair_launches_the_managed_claude(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = tmp / "bin"
        log = tmp / "tmux.log"
        write_exe(bin_dir / "tmux", f'echo "$*" >> "{log}"\nexit 0\n')
        write_exe(bin_dir / "claude", "exit 1\n")
        write_exe(bin_dir / "pgrep", "exit 1\n")
        write_exe(bin_dir / "npm", f"""PREFIX=""
while [ $# -gt 0 ]; do [ "$1" = --prefix ] && PREFIX="$2"; shift; done
mkdir -p "$PREFIX/bin"
printf '#!/bin/sh\\ncase "$1" in --version) echo 2.4.0;; --help) echo "{CLAUDE_FLAGS}";; esac\\n' > "$PREFIX/bin/claude"
chmod +x "$PREFIX/bin/claude"
""")
        env = {"HOME": str(tmp / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin",
               "SUTANDO_RUNTIMES_DIR": str(tmp / "runtimes"), "SUTANDO_RUNTIME_SYSTEM_DIRS": "",
               "SUTANDO_RUNTIME_WAIT_S": "0", "SUTANDO_TMUX_SOCKET": str(tmp / "sock"),
               "SUTANDO_SKIP_AUTH_PREFLIGHT": "1", "CLAUDE_CONFIG_DIR": str(tmp / "cfg"),
               "SUTANDO_RUNTIME_REPAIR_CONSENT": "1"}
        r = subprocess.run(["bash", str(launcher_copy(tmp) / "src/agent/claude/cli/start-cli.sh")],
                           env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=180)
        self.assertIn("repair succeeded: claude 2.4.0", r.stderr)
        managed = tmp / "runtimes" / "bin" / "claude"
        self.assertIn(f" {managed} --name sutando-core ", log.read_text(), r.stderr[-2000:])

if __name__ == "__main__":
    unittest.main(verbosity=1)
