"""Regression guard for fix(health): broaden Sutando-app pgrep to match
installed .app path (port of sonichi/sutando#1069).

Pre-fix: health-check.py and dashboard.py used the hardcoded pattern
`src/Sutando/Sutando` (dev-build only). On a distributed .app install the
binary lives at `/Applications/Sutando.app/Contents/MacOS/Sutando` — the
old pattern never matched, so health-check always reported "not running".

Fix: use `(Sutando|MacOS)/Sutando` regex that matches both paths, and
check `app_bin.exists()` alongside `dev_bin.exists()` so the check runs
even when no dev binary is present.

These are source-grep architectural assertions — no subprocess spawn needed.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
HEALTH_SRC = (REPO / "src" / "health-check.py").read_text()
DASH_SRC = (REPO / "src" / "dashboard.py").read_text()

# The pgrep pattern string as it appears in the source code
BROAD_PATTERN = "(Sutando|MacOS)/Sutando"


class HealthCheckPgrepBroadened(unittest.TestCase):
    def test_health_check_uses_broad_pgrep_pattern(self):
        """health-check.py must match both dev-build and .app paths."""
        self.assertIn(
            BROAD_PATTERN,
            HEALTH_SRC,
            "health-check.py still uses the narrow 'src/Sutando/Sutando' pgrep "
            "pattern. It must use '(Sutando|MacOS)/Sutando' to match both the "
            "dev-built binary and /Applications/Sutando.app/Contents/MacOS/Sutando.",
        )

    def test_health_check_does_not_use_narrow_pattern_in_pgrep(self):
        """The old hardcoded pattern must not appear in the pgrep call."""
        # pgrep with the narrow old pattern — the fix replaces it.
        self.assertNotIn(
            '"/usr/bin/pgrep", "-f", "Sutando/Sutando"',
            HEALTH_SRC,
            "health-check.py still passes the narrow 'Sutando/Sutando' string "
            "to pgrep — replace it with '(Sutando|MacOS)/Sutando'.",
        )

    def test_health_check_checks_app_bin_exists(self):
        """Must check for the installed .app binary path, not just dev_bin."""
        self.assertIn(
            "app_bin.exists()",
            HEALTH_SRC,
            "health-check.py must check app_bin.exists() so the Sutando-app "
            "health check runs on distributed .app installs where the dev "
            "binary is absent.",
        )

    def test_health_check_defines_app_bin_path(self):
        """The .app binary path must be explicitly defined."""
        self.assertIn(
            "/Applications/Sutando.app/Contents/MacOS/Sutando",
            HEALTH_SRC,
            "health-check.py must define the distributed .app binary path "
            "(/Applications/Sutando.app/Contents/MacOS/Sutando) so the "
            "existence check can gate properly.",
        )

    def test_staleness_check_gated_on_dev_bin(self):
        """mark_stale_if_outdated must only run when dev_bin exists."""
        # The pattern `if dev_bin.exists():` must appear before the
        # mark_stale_if_outdated call to avoid comparing build mtimes
        # on an installed .app where the comparison is meaningless.
        self.assertRegex(
            HEALTH_SRC,
            r"if dev_bin\.exists\(\):\s+mark_stale_if_outdated",
            "health-check.py must gate mark_stale_if_outdated on "
            "dev_bin.exists() — comparing mtimes on a distributed .app "
            "install is meaningless and should be skipped.",
        )

    def test_dashboard_uses_broad_pgrep_pattern(self):
        """dashboard.py must also use the broadened pgrep pattern."""
        self.assertIn(
            BROAD_PATTERN,
            DASH_SRC,
            "dashboard.py still uses the narrow pgrep pattern. It must use "
            "'(Sutando|MacOS)/Sutando' to show accurate running status for "
            "distributed .app installs.",
        )

    def test_dashboard_does_not_use_narrow_pattern(self):
        """The old hardcoded src/Sutando/Sutando pattern must not appear."""
        self.assertNotIn(
            '"src/Sutando/Sutando"',
            DASH_SRC,
            "dashboard.py still uses the narrow 'src/Sutando/Sutando' pgrep "
            "pattern — replace it with '(Sutando|MacOS)/Sutando'.",
        )


if __name__ == "__main__":
    unittest.main()
