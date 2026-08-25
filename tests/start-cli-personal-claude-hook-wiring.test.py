#!/usr/bin/env python3
"""
start-cli.sh must call scripts/install-personal-claude-hook.sh on every
launch, not just via startup.sh.

Direct restarts (menu bar, health-check recovery, manual --restart,
supervisor) documented in start-cli.sh do not pass through startup.sh --
the same gap that was already fixed there once for
SUTANDO_SELF_DEVELOPMENT_ENABLED propagation and the quota-proxy wiring.
The PERSONAL_CLAUDE.md compaction hook's registration
(scripts/install-personal-claude-hook.sh) never got the same treatment: a
core whose first-ever launch is a direct restart path would never get the
hook installed. This test asserts start-cli.sh invokes the (idempotent)
installer unconditionally, before it ever reaches runtime dispatch.

Hermetic: a real scripts/install-personal-claude-hook.sh is stubbed out so
this test only proves the CALL happens, not the installer's own behavior
(that is tests/personal-claude-compact-hook.test.py's job).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class StartCliPersonalClaudeHookWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        for rel in ("src/agent/start-cli.sh", "scripts/sutando-config.sh"):
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)

        # Stub the installer: record that it ran, then exit 0 (idempotent
        # no-op on a real re-run, so `|| true` in the caller never masks a
        # real failure here).
        installer = self.root / "scripts" / "install-personal-claude-hook.sh"
        self.marker = self.root / "installer-ran.marker"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            f"echo ran >> '{self.marker}'\n"
        )
        installer.chmod(0o755)

        # Stub core-runtime resolution to fail immediately AFTER the
        # install call — proves ordering (installer runs before dispatch)
        # without needing a real runtime launcher or tmux session.
        config = self.root / "scripts" / "sutando-config.sh"
        config.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "core-runtime" ]; then\n'
            '  echo "start-cli: intentional test stop" >&2\n'
            "  exit 7\n"
            "fi\n"
        )
        config.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def test_installer_runs_before_runtime_dispatch(self):
        result = subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/start-cli.sh")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertTrue(
            self.marker.exists(),
            "start-cli.sh did not invoke install-personal-claude-hook.sh "
            f"(stderr: {result.stderr})",
        )
        self.assertEqual(self.marker.read_text().count("ran"), 1)
        # start-cli.sh maps any core-runtime failure to exit 1; reaching
        # that (not some earlier abort) proves the install call ran BEFORE
        # dispatch, not that dispatch never happens for real.
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed to resolve core runtime", result.stderr)

    def test_installer_failure_does_not_abort_launch(self):
        installer = self.root / "scripts" / "install-personal-claude-hook.sh"
        installer.write_text("#!/usr/bin/env bash\nexit 1\n")
        installer.chmod(0o755)
        result = subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/start-cli.sh")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # set -euo pipefail would abort the whole launcher on a bare failed
        # call; `|| true` is what keeps a broken/missing installer from
        # taking the core down with it. Reaching start-cli.sh's OWN
        # core-runtime error message (not a raw `set -e` abort with no
        # message) is what proves that.
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("failed to resolve core runtime", result.stderr)


if __name__ == "__main__":
    unittest.main()
