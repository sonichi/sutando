#!/usr/bin/env python3
"""util_paths._host_label precedence: env → scutil LocalHostName → hostname.

Guards the DHCP-hostname-drift fix: a drifting `hostname` must not override the
stable Bonjour LocalHostName on macOS. Run:
  python3 tests/host-label-scutil.test.py
"""
import os, sys, unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import util_paths  # noqa: E402


def _scutil(rc=0, out="Chis-MacBook-Pro"):
    return MagicMock(returncode=rc, stdout=out)


class HostLabelPrecedence(unittest.TestCase):
    def setUp(self):
        # Clear both env vars by default; tests opt in.
        self._patcher = patch.dict(os.environ, {}, clear=False)
        for k in ("SUTANDO_HOST_LABEL", "SUTANDO_HOST_OVERRIDE"):
            os.environ.pop(k, None)

    def test_env_label_wins(self):
        with patch.dict(os.environ, {"SUTANDO_HOST_LABEL": "Pinned"}), \
             patch.object(util_paths.subprocess, "run") as run:
            self.assertEqual(util_paths._host_label(), "Pinned")
            run.assert_not_called()  # env short-circuits before scutil

    def test_legacy_override_env_honored(self):
        with patch.dict(os.environ, {"SUTANDO_HOST_OVERRIDE": "Legacy"}), \
             patch.object(util_paths.subprocess, "run") as run:
            self.assertEqual(util_paths._host_label(), "Legacy")
            run.assert_not_called()

    def test_scutil_localhostname_over_hostname(self):
        with patch.object(util_paths.subprocess, "run", return_value=_scutil(0, "Chis-MacBook-Pro\n")), \
             patch.object(util_paths.socket, "gethostname", return_value="Chis-MBP.hsd1.wa.comcast.net"):
            self.assertEqual(util_paths._host_label(), "Chis-MacBook-Pro")

    def test_scutil_nonzero_falls_back_to_hostname(self):
        with patch.object(util_paths.subprocess, "run", return_value=_scutil(1, "")), \
             patch.object(util_paths.socket, "gethostname", return_value="box.local"):
            self.assertEqual(util_paths._host_label(), "box")

    def test_scutil_missing_falls_back_to_hostname(self):
        # Linux / scutil absent → OSError → hostname.
        with patch.object(util_paths.subprocess, "run", side_effect=FileNotFoundError), \
             patch.object(util_paths.socket, "gethostname", return_value="linuxhost"):
            self.assertEqual(util_paths._host_label(), "linuxhost")

    def test_scutil_empty_output_falls_back(self):
        with patch.object(util_paths.subprocess, "run", return_value=_scutil(0, "  \n")), \
             patch.object(util_paths.socket, "gethostname", return_value="fallback.local"):
            self.assertEqual(util_paths._host_label(), "fallback")

    def test_scutil_timeout_falls_back(self):
        import subprocess as _sp
        with patch.object(util_paths.subprocess, "run", side_effect=_sp.TimeoutExpired("scutil", 2)), \
             patch.object(util_paths.socket, "gethostname", return_value="slow.local"):
            self.assertEqual(util_paths._host_label(), "slow")


if __name__ == "__main__":
    unittest.main()
