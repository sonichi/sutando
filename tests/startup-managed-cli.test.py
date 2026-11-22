#!/usr/bin/env python3
"""The real src/startup.sh finds a Sutando-managed core CLI that is not on PATH.

Startup runs from a temp copy of the repo, so its workspace is a temp folder.
The only claude or codex is the managed one under SUTANDO_RUNTIMES_DIR; nothing
of that name is on PATH. Startup must pass its CLI checks instead of exiting
"not installed" (127) or "CLI not found". The copy's accessibility probe is
replaced by a stop, so startup ends right after those checks, before any service.

Run: python3 tests/startup-managed-cli.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLAGS = {
    "claude": ("--name --dangerously-skip-permissions --add-dir --settings --remote-control "
               "--chrome --plugin-dir --resume --session-id"),
    "codex": "--cd --add-dir --sandbox --ask-for-approval --search --no-alt-screen --config --model",
}
VERSION = {"claude": "2.1.0 (Claude Code)", "codex": "codex-cli 0.160.1"}
STOP = "startup-test: passed the core CLI checks"


def write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


class StartupFindsTheManagedCli(unittest.TestCase):
    def run_startup(self, runtime: str, login_rc: int = 0, script: str = "startup.sh"):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        for rel in ("src", "scripts"):
            shutil.copytree(REPO / rel, repo / rel, symlinks=True)
        for rel in ("sutando.config.json", "package.json"):
            shutil.copy2(REPO / rel, repo / rel)
        (repo / "node_modules").mkdir()
        (repo / ".env").write_text(f"SUTANDO_CORE_RUNTIME={runtime}\n")
        (repo / "src" / "accessibility_probe.sh").write_text(
            f'accessibility_probe() {{ echo "{STOP}"; exit 0; }}\n')
        stubs = tmp / "stubs"
        for name in ("npm", "npx", "fswatch", "pgrep", "screencapture", "caffeinate", "tmux",
                     "launchctl", "osascript"):
            write_exe(stubs / name, "exit 0\n")
        write_exe(stubs / "node", 'echo "v22.12.0"\n')
        runtimes = tmp / "runtimes"
        log = tmp / "cli.log"
        write_exe(runtimes / runtime / "versions" / "1" / "bin" / runtime, f"""echo "$*" >> "{log}"
case "${{1:-}}" in
  --version) echo "{VERSION[runtime]}" ;;
  --help) echo "{FLAGS[runtime]}" ;;
  login) exit {login_rc} ;;
esac
""")
        (runtimes / "bin").mkdir()
        os.symlink(runtimes / runtime / "versions" / "1" / "bin" / runtime, runtimes / "bin" / runtime)
        py = os.path.realpath(sys.executable)
        os.symlink(py, stubs / "python3")
        env = {"HOME": str(tmp / "home"), "PATH": f"{stubs}:/usr/bin:/bin",
               "SUTANDO_PY": py, "SUTANDO_RUNTIMES_DIR": str(runtimes),
               "SUTANDO_RUNTIME_SYSTEM_DIRS": "", "SUTANDO_RUNTIME_WAIT_S": "0",
               "SUTANDO_CORE_RUNTIME": runtime,
               "CODEX_HOME": str(tmp / "codex-home"), "SUTANDO_SKIP_AUTH_PREFLIGHT":
               "1" if runtime == "claude" else "0", "LANG": "en_US.UTF-8", "TMPDIR": str(tmp)}
        (tmp / "home").mkdir()
        self.assertIsNone(shutil.which(runtime, path=env["PATH"]), f"{runtime} must not be on PATH")
        r = subprocess.run(["bash", str(repo / "src" / script)], env=env, cwd=str(repo),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
        return r, (log.read_text() if log.exists() else ""), runtimes / "bin" / runtime

    def test_codex_only_managed_startup_passes_its_cli_checks(self):
        r, calls, managed = self.run_startup("codex")
        out = r.stdout + r.stderr
        self.assertNotIn("not installed", out)
        self.assertNotIn("CLI not found", out)
        self.assertIn(f"using {managed} (0.160.1)", out)
        self.assertIn("login status", calls)
        self.assertIn(STOP, r.stdout, out[-3000:])
        self.assertEqual(r.returncode, 0, out[-3000:])

    def test_claude_only_managed_startup_passes_its_cli_checks(self):
        r, _calls, managed = self.run_startup("claude")
        out = r.stdout + r.stderr
        self.assertNotIn("CLI not found", out)
        self.assertIn(f"using {managed} (2.1.0)", out)
        self.assertIn(STOP, r.stdout, out[-3000:])
        self.assertEqual(r.returncode, 0, out[-3000:])

    def test_codex_only_managed_but_logged_out_stops_with_the_login_step(self):
        r, _calls, managed = self.run_startup("codex", login_rc=1)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 4, out[-3000:])
        self.assertIn(f"{managed} login", out)
        self.assertNotIn(STOP, r.stdout)


    def test_verify_setup_finds_the_managed_codex_and_its_login(self):
        r, _calls, managed = self.run_startup("codex", script="verify-setup.sh")
        self.assertIn(f"✓ codex core CLI ({managed})", r.stdout, r.stdout[-3000:])
        r, _calls, _managed = self.run_startup("codex", login_rc=1, script="verify-setup.sh")
        self.assertIn("✗ Codex CLI is not authenticated (run: codex login)", r.stdout)


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=1))
