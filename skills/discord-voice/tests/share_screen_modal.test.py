#!/usr/bin/env python3
"""
Tests for skills/discord-voice/scripts/share-screen-modal.py

Coverage:
  COORDS           — expected keys present, values are 2-tuples of ints
  main (--dry-run) — exits 0, prints all COORDS keys
  main (--stop)    — exits 0, calls click once
  main (--modal)   — exits 0, calls click 3 times, skips activate_mcp_chrome
  main (--full)    — exits 0, calls click 5 times

Note: Quartz is mocked at module load so no native framework needed.

Run: python3 skills/discord-voice/tests/share_screen_modal.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent.parent.parent

# Inject fake Quartz before loading the module (module-level import guard)
_fake_quartz = types.ModuleType("Quartz")
_fake_quartz.CGEventCreateMouseEvent = MagicMock(return_value=object())
_fake_quartz.CGEventPost = MagicMock()
_fake_quartz.kCGEventLeftMouseDown = 1
_fake_quartz.kCGEventLeftMouseUp = 2
_fake_quartz.kCGHIDEventTap = 0
_fake_quartz.kCGMouseButtonLeft = 0
sys.modules.setdefault("Quartz", _fake_quartz)

_spec = importlib.util.spec_from_file_location(
    "share_screen_modal",
    REPO / "skills" / "discord-voice" / "scripts" / "share-screen-modal.py",
)
ssm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssm)


# ── COORDS ────────────────────────────────────────────────────────────────────

class TestCoords(unittest.TestCase):
    _EXPECTED_KEYS = {
        "discord_share_button",
        "entire_screen_tab",
        "thumbnail",
        "share_button",
    }

    def test_all_keys_present(self):
        self.assertTrue(self._EXPECTED_KEYS.issubset(set(ssm.COORDS.keys())))

    def test_values_are_2_tuples_of_ints(self):
        for name, val in ssm.COORDS.items():
            self.assertIsInstance(val, tuple, f"{name} not a tuple")
            self.assertEqual(len(val), 2, f"{name} not a 2-tuple")
            self.assertIsInstance(val[0], int, f"{name}[0] not int")
            self.assertIsInstance(val[1], int, f"{name}[1] not int")


# ── main ──────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def _run_main(self, argv):
        """Run main() with given argv; return (return_code, stdout)."""
        with patch("sys.argv", ["share-screen-modal.py"] + argv), \
             patch("sys.stdout", new_callable=StringIO) as out, \
             patch("time.sleep"):  # skip real sleeps
            try:
                code = ssm.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_dry_run_exits_0(self):
        code, _ = self._run_main(["--dry-run"])
        self.assertEqual(code, 0)

    def test_dry_run_prints_all_coord_names(self):
        _, out = self._run_main(["--dry-run"])
        for key in ssm.COORDS:
            self.assertIn(key, out)

    def test_stop_mode_exits_0(self):
        with patch.object(ssm, "click") as mock_click, \
             patch.object(ssm, "activate_mcp_chrome"):
            code, _ = self._run_main(["--stop"])
        self.assertEqual(code, 0)

    def test_stop_mode_clicks_once(self):
        with patch.object(ssm, "click") as mock_click, \
             patch.object(ssm, "activate_mcp_chrome"):
            self._run_main(["--stop"])
        self.assertEqual(mock_click.call_count, 1)

    def test_modal_mode_clicks_three_times(self):
        with patch.object(ssm, "click") as mock_click, \
             patch.object(ssm, "activate_mcp_chrome") as mock_activate:
            self._run_main(["--modal"])
        self.assertEqual(mock_click.call_count, 3)

    def test_modal_mode_skips_activate_chrome(self):
        with patch.object(ssm, "click"), \
             patch.object(ssm, "activate_mcp_chrome") as mock_activate:
            self._run_main(["--modal"])
        mock_activate.assert_not_called()

    def test_full_mode_clicks_four_times(self):
        # discord_share_button + entire_screen_tab + thumbnail + share_button
        with patch.object(ssm, "click") as mock_click, \
             patch.object(ssm, "activate_mcp_chrome"):
            self._run_main([])  # default = --full
        self.assertEqual(mock_click.call_count, 4)

    def test_full_mode_exits_0(self):
        with patch.object(ssm, "click"), \
             patch.object(ssm, "activate_mcp_chrome"):
            code, _ = self._run_main([])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
