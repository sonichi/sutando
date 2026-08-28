#!/usr/bin/env python3
"""The notifier's prompt embeds a command Codex will RUN. Every interpolated
path must survive a shell, or an install whose repo or workspace contains a
space loses every managed completion."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOTIFIER = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"


class EmittedCommandQuotingTest(unittest.TestCase):
    def _emit_and_run(self, repo_dir: Path, ws: Path):
        """Drive the real --event path, take the command it told Codex to run,
        and actually run it. Returns (rc, stderr, result_exists)."""
        (ws / "tasks").mkdir(parents=True)
        (ws / "results").mkdir()
        (ws / "tasks" / "task-probe.txt").write_text("id: task-probe\n")
        binp = ws.parent / "bin"
        binp.mkdir(exist_ok=True)
        log = ws.parent / "tmux.log"
        (binp / "tmux").write_text(
            f'#!/bin/bash\nprintf "%s\\n" "$*" >> {log!s}\nexit 0\n')
        os.chmod(binp / "tmux", 0o755)

        env = dict(os.environ)
        env.update({
            "PATH": f"{binp}:{env['PATH']}",
            "SUTANDO_TASKS_DIR": str(ws / "tasks"),
            "SUTANDO_RESULTS_DIR": str(ws / "results"),
            "SUTANDO_RESULT_PAIRING_DIR": str(ws / "state" / "result-pairing"),
            "SUTANDO_TMUX_SOCKET": str(ws.parent / "s.sock"),
        })
        script = repo_dir / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"
        subprocess.run(["bash", str(script), "--event", "task-probe.txt"],
                       env=env, capture_output=True, text=True, timeout=60)

        txt = log.read_text() if log.exists() else ""
        # A correctly-quoted path contains `\ `, which \S+ cannot cross —
        # the naive pattern reports "no command" on exactly the fixed case.
        tok = r"(?:\\.|\S)+"
        cmd = re.search(
            rf"(python3 {tok}result_write\.py write .*?--receipts-dir {tok})", txt)
        self.assertIsNotNone(cmd, "the notifier emitted no write command")
        # Take the required first line from the prompt itself rather than
        # re-deriving the id — a re-typed probe drifts from what is instructed.
        first = re.search(r"FIRST line exactly '([^']+)'", txt)
        self.assertIsNotNone(first, "prompt no longer states the pairing line")
        run = subprocess.run(["bash", "-c", cmd.group(1)],
                             input=first.group(1) + "\nthe answer\n",
                             capture_output=True, text=True)
        return run.returncode, run.stderr, (ws / "results" / "task-probe.txt").exists()

    def test_workspace_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as td:
            rc, err, wrote = self._emit_and_run(
                REPO, Path(td) / "workspace with spaces")
        self.assertEqual(rc, 0, f"emitted command failed: {err.strip()[:120]}")
        self.assertNotIn("usage:", err, "arguments were split by the shell")
        self.assertTrue(wrote, "no result written")

    def test_repo_path_with_spaces(self):
        """$RESULT_WRITER is derived from $REPO, so a spaced repo breaks the
        same command through a different variable."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo with spaces"
            cli = repo / "src" / "agent" / "codex" / "cli"
            cli.mkdir(parents=True)
            shutil.copy2(NOTIFIER, cli / "task-notifier.sh")
            shutil.copy2(REPO / "src" / "result_write.py",
                         repo / "src" / "result_write.py")
            (repo / "scripts").mkdir()
            shutil.copy2(REPO / "scripts" / "sutando-config.sh",
                         repo / "scripts" / "sutando-config.sh")
            rc, err, wrote = self._emit_and_run(repo, Path(td) / "ws")
        self.assertEqual(rc, 0, f"emitted command failed: {err.strip()[:120]}")
        self.assertTrue(wrote, "no result written")


if __name__ == "__main__":
    unittest.main()
