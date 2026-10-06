#!/usr/bin/env python3
"""Check that a Codex worker reconciles the shared reset timer at launch."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]


class CodexWorkerResetLauncherTests(unittest.TestCase):
    def test_worker_installs_timer_with_workspace_home_and_disable_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            workspace = Path(tmp) / "workspace"
            bin_dir = Path(tmp) / "bin"
            for path in (home, workspace, bin_dir):
                path.mkdir()
            for rel in ("skills/worker-pool/scripts/launch-codex-worker-session.sh",
                        "scripts/python-binary.sh"):
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / rel, dest)
            config = root / "scripts/sutando-config.sh"
            config.write_text(
                '#!/bin/sh\n'
                'case "$1" in\n'
                '  core-config-dir-env-name) printf "CODEX_HOME" ;;\n'
                '  core-config-dir-value) printf "%s/codex" "$HOME" ;;\n'
                'esac\n'
            )
            timer = root / "skills/proactive-loop/scripts/codex-auto-reset-timer.py"
            timer.parent.mkdir(parents=True)
            timer.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['RESET_LOG']).write_text(json.dumps({"
                "'args': sys.argv[1:], 'enabled': os.environ.get('SUTANDO_CODEX_AUTO_RESET_ENABLED')}))\n"
            )
            watcher = root / "skills/worker-pool/scripts/worker-watcher-supervisor.sh"
            watcher.write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / "codex").write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / "fswatch").write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
            (bin_dir / "tmux").write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$TMUX_LOG"\n'
                '[ "$1" = -S ] && shift 2\n'
                'case "$1" in\n'
                '  has-session) [ -f "$TMUX_SESSION_STATE" ] ;;\n'
                '  new-session) touch "$TMUX_SESSION_STATE" ;;\n'
                'esac\n'
            )
            for path in bin_dir.iterdir():
                path.chmod(0o755)
            reset_log = Path(tmp) / "reset.json"
            tmux_log = Path(tmp) / "tmux.log"
            env = {**os.environ, "HOME": str(home),
                   "PATH": f"{bin_dir}:/usr/bin:/bin",
                   "SUTANDO_PY": sys.executable,
                   "SUTANDO_INSTANCE_ID": "worker-test",
                   "SUTANDO_TMUX_SESSION": "worker-session",
                   "SUTANDO_TASKS_DIR": str(workspace / "deliveries/worker-test"),
                   "SUTANDO_WORKSPACE_DIR": str(workspace),
                   "SUTANDO_RESULTS_DIR": str(workspace / "results"),
                   "SUTANDO_INBOX_RESOLVER": str(root / "resolver"),
                   "SUTANDO_POOL_DELIVERY_SCRIPT": str(root / "delivery.py"),
                   "SUTANDO_CODEX_AUTO_RESET_ENABLED": "0",
                   "RESET_LOG": str(reset_log), "TMUX_LOG": str(tmux_log),
                   "TMUX_SESSION_STATE": str(Path(tmp) / "session-started")}
            result = subprocess.run(
                ["bash", str(root / "skills/worker-pool/scripts/launch-codex-worker-session.sh")],
                cwd=root, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(reset_log.read_text()), {
                "args": ["ensure", "--workspace", str(workspace),
                         "--codex-home", str(home / "codex")],
                "enabled": "0",
            })
            self.assertIn("-e SUTANDO_CODEX_AUTO_RESET_ENABLED=0", tmux_log.read_text())


if __name__ == "__main__":
    unittest.main()
